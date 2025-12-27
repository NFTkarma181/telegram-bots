# contents of file
import os
import asyncio
import time
import uuid
import json
import logging
import random
from typing import Optional

import requests
from telethon import events
from telethon.errors import (
    UserIsBlockedError,
    PeerIdInvalidError,
    RpcCallFailError,
)
from telethon.tl.types import MessageEntityTextUrl, MessageEntityCustomEmoji

# ==========================
# Основная конфигурация — поставьте BOT_TOKEN и (опционально) ADMIN_ID
BOT_TOKEN = "8558132355:AAEOyM0kqHzP7g3olZE_fngicMs4HpLIOPw"            # вставьте токен BotFather или установите BOT_TOKEN в окружении
PROVIDER_TOKEN = ""

# Unicode emoji (fallback). Если указан INVOICE_CUSTOM_EMOJI_ID, будет использован custom emoji вместо этого.
INVOICE_EMOJI = "⭐"

# Если хотите использовать custom premium emoji, укажите его ID (int) здесь или через окружение:
# ВАШ НОВЫЙ ID:
INVOICE_CUSTOM_EMOJI_ID = 5999031072887673336

# Admin ID: можно указать прямо здесь или через ADMIN_ID в окружении
ADMIN_ID = 7738435649

# Остальное
CURRENCY = "XTR"
AMOUNT_MULTIPLIER = 1
MAX_INVOICE_ATTEMPTS = 6
REQUEST_TIMEOUT = 20
REFUND_API_URL = ""
REFUND_API_KEY = ""
DELETION_DELAY = 3.0
BOT_POLL_INTERVAL = 1.0
# ==========================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("telethon-invoice")

# Load BOT_TOKEN, ADMIN_ID, INVOICE_CUSTOM_EMOJI_ID from env if not set
if not BOT_TOKEN:
    BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

if ADMIN_ID is None:
    admin_env = os.getenv("ADMIN_ID") or os.getenv("TELEGRAM_ADMIN_ID")
    if admin_env:
        try:
            ADMIN_ID = int(admin_env)
        except Exception:
            log.warning("ADMIN_ID из окружения некорректен: %r", admin_env)
            ADMIN_ID = None

if INVOICE_CUSTOM_EMOJI_ID is None:
    custom_env = os.getenv("INVOICE_CUSTOM_EMOJI_ID")
    if custom_env:
        try:
            INVOICE_CUSTOM_EMOJI_ID = int(custom_env)
        except Exception:
            log.warning("INVOICE_CUSTOM_EMOJI_ID из окружения некорректен: %r", custom_env)
            INVOICE_CUSTOM_EMOJI_ID = None

if not BOT_TOKEN:
    log.warning("BOT_TOKEN не задан; вызовы Bot API будут падать без валидного токена.")
if ADMIN_ID is None:
    log.info("ADMIN_ID не задан — команда .refund будет недоступна или доступна всем.")


# module-level client (set in setup)
client = None  # type: Optional[object]

# in-memory invoice mapping
INVOICE_MAP = {}
INVOICE_MAP_LOCK = asyncio.Lock()

# handlers and background task references
_REGISTERED_HANDLERS = []
_BOT_TASK = None


# ---- Bot API helpers ----
def _call_bot_api_sync(method: str, data: dict = None, files: dict = None) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        if files:
            r = requests.post(url, data=(data or {}), files=files, timeout=REQUEST_TIMEOUT)
        else:
            r = requests.post(url, data=(data or {}), timeout=REQUEST_TIMEOUT)
    except Exception as e:
        log.exception("HTTP request to Bot API failed for %s", method)
        return {"ok": False, "description": f"HTTP request failed: {e}"}
    try:
        if r.status_code != 200:
            log.warning("Bot API %s returned HTTP %s: %s", method, r.status_code, r.text)
        return r.json()
    except Exception:
        log.exception("Invalid JSON from Bot API %s: %s", method, r.text)
        return {"ok": False, "description": f"Invalid JSON response from Bot API: {r.text}", "http_status": r.status_code}


async def call_bot_api(method: str, data: dict = None, files: dict = None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _call_bot_api_sync(method, data, files))


# ---- Mapping helpers ----
async def register_invoice(payload: str, info: dict):
    async with INVOICE_MAP_LOCK:
        INVOICE_MAP[payload] = info
        log.debug("Registered invoice payload=%s -> %s", payload, info)


async def pop_invoice(payload: str) -> Optional[dict]:
    async with INVOICE_MAP_LOCK:
        return INVOICE_MAP.pop(payload, None)


# ---- schedule delete ----
async def _schedule_delete(entity, message_id, delay):
    try:
        await asyncio.sleep(delay)
        if client is not None:
            await client.delete_messages(entity=entity, message_ids=[message_id])
            log.debug("Deleted ephemeral message %s:%s", entity, message_id)
    except Exception:
        log.exception("Failed to delete ephemeral message %s:%s", entity, message_id)


def schedule_delete(entity, message_id, delay=DELETION_DELAY):
    try:
        asyncio.create_task(_schedule_delete(entity, message_id, delay))
    except Exception:
        log.exception("Failed to schedule delete for %s:%s", entity, message_id)


# ---- createInvoiceLink (async) ----
async def create_invoice_link_via_bot(title: str, description: str, amount: int, base_payload: str,
                                      max_attempts: int = MAX_INVOICE_ATTEMPTS, provider_token: str = None) -> dict:
    method = "createInvoiceLink"
    prices = [{"label": title, "amount": int(amount) * AMOUNT_MULTIPLIER}]
    provider_token = provider_token if provider_token is not None else PROVIDER_TOKEN

    for attempt in range(1, max_attempts + 1):
        payload = f"{base_payload}_{int(time.time())}_{uuid.uuid4().hex[:8]}_{random.randint(0,9999)}_a{attempt}"
        data = {
            "title": title,
            "description": description,
            "payload": payload,
            "currency": CURRENCY,
            "prices": json.dumps(prices),
        }
        if provider_token:
            data["provider_token"] = provider_token

        resp = await call_bot_api(method, data)
        log.debug("createInvoiceLink attempt %s resp=%s", attempt, resp)

        if resp.get("ok"):
            result = resp.get("result")
            if not isinstance(result, dict):
                resp["result"] = {"_raw_result": result}
            resp["result"]["used_payload"] = payload
            return resp

        desc = str(resp.get("description", "")).lower()
        log.info("createInvoiceLink failed attempt %s: %s", attempt, desc)
        if "duplicate" in desc or "form_submit_duplicate" in desc or "form_submit" in desc:
            delay = 0.2 + random.random() * 0.5
            log.warning("Detected duplicate/form_submit error; retrying after %.2fs (attempt %s)", delay, attempt + 1)
            await asyncio.sleep(delay)
            continue

        return resp

    return {"ok": False, "description": "max attempts reached creating invoice (possible duplicate form)"}


# ---- Refund helper ----
async def perform_refund(user_id: str, telegram_payment_charge_id: str) -> dict:
    loop = asyncio.get_running_loop()
    if REFUND_API_URL:
        payload = {"user_id": user_id, "telegram_payment_charge_id": telegram_payment_charge_id}
        headers = {"Content-Type": "application/json"}
        if REFUND_API_KEY:
            headers["x-api-key"] = REFUND_API_KEY
        try:
            def _req():
                return requests.post(REFUND_API_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            r = await loop.run_in_executor(None, _req)
            try:
                data = r.json()
            except Exception:
                return {"ok": False, "description": f"Invalid JSON from refund endpoint: {r.text}"}
            return data
        except Exception as e:
            return {"ok": False, "description": f"HTTP request to refund endpoint failed: {e}"}
    else:
        return await call_bot_api("refundStarPayment", {"user_id": user_id, "telegram_payment_charge_id": telegram_payment_charge_id})


# ---- bot poller ----
async def bot_updates_task():
    log.info("Starting bot_updates_task...")
    url_get = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    offset = None
    loop = asyncio.get_running_loop()
    while True:
        try:
            params = {"timeout": 20, "allowed_updates": json.dumps(["pre_checkout_query", "message"])}
            if offset:
                params["offset"] = offset

            def _get():
                try:
                    return requests.get(url_get, params=params, timeout=REQUEST_TIMEOUT + 5)
                except Exception as e:
                    return e

            r = await loop.run_in_executor(None, _get)
            if isinstance(r, Exception):
                log.warning("getUpdates request exception: %s", r)
                await asyncio.sleep(BOT_POLL_INTERVAL)
                continue

            try:
                data = r.json()
            except Exception:
                log.warning("Invalid JSON from getUpdates: %s", r.text)
                await asyncio.sleep(BOT_POLL_INTERVAL)
                continue

            if not data.get("ok"):
                log.warning("getUpdates returned not ok: %s", data)
                await asyncio.sleep(BOT_POLL_INTERVAL)
                continue

            results = data.get("result", [])
            if not results:
                continue

            for upd in results:
                offset = upd["update_id"] + 1

                if "pre_checkout_query" in upd:
                    pcq = upd["pre_checkout_query"]
                    pcq_id = pcq.get("id")
                    from_id = pcq.get("from", {}).get("id")
                    log.info("Received pre_checkout_query id=%s from user %s", pcq_id, from_id)

                    def _answer():
                        return _call_bot_api_sync("answerPreCheckoutQuery", {"pre_checkout_query_id": pcq_id, "ok": True})

                    resp = await loop.run_in_executor(None, _answer)
                    if not resp.get("ok"):
                        log.error("answerPreCheckoutQuery failed: %s", resp)
                    else:
                        log.debug("PreCheckoutQuery answered ok id=%s", pcq_id)

                if "message" in upd:
                    msg = upd["message"]
                    if "successful_payment" in msg:
                        sp = msg["successful_payment"]
                        from_user = msg.get("from", {})
                        payer_id = from_user.get("id")
                        currency = sp.get("currency")
                        total = sp.get("total_amount")
                        invoice_payload = sp.get("invoice_payload")
                        log.info("Successful payment from %s amount=%s %s payload=%s", payer_id, total, currency, invoice_payload)

                        mapping = None
                        if invoice_payload:
                            async with INVOICE_MAP_LOCK:
                                mapping = INVOICE_MAP.pop(invoice_payload, None)

                        if mapping:
                            log.info("Found mapping for payload %s: %s", invoice_payload, mapping)
                            user_chat_id = mapping.get("user_chat_id")
                            user_msg_id = mapping.get("user_msg_id")
                            if user_chat_id and user_msg_id:
                                try:
                                    if client is not None:
                                        await client.delete_messages(entity=user_chat_id, message_ids=[user_msg_id])
                                    log.debug("Deleted user invoice message %s:%s", user_chat_id, user_msg_id)
                                except Exception:
                                    log.exception("Failed to delete user invoice message via Telethon")
                            bot_chat_id = mapping.get("bot_chat_id")
                            bot_msg_id = mapping.get("bot_msg_id")
                            if bot_chat_id and bot_msg_id:
                                def _del_bot():
                                    return _call_bot_api_sync("deleteMessage", {"chat_id": bot_chat_id, "message_id": bot_msg_id})
                                dresp = await loop.run_in_executor(None, _del_bot)
                                if not dresp.get("ok"):
                                    log.warning("Failed to delete bot invoice message: %s", dresp)
                                else:
                                    log.debug("Deleted bot invoice message %s:%s", bot_chat_id, bot_msg_id)

                            try:
                                # Send thank-you message; if mapping contains thank_entities, pass them using formatting_entities
                                thank_text = mapping.get("thank_text") or "Спасибо за покупку!"
                                target_chat = mapping.get("user_chat_id") or payer_id
                                if client is not None:
                                    thank_entities = mapping.get("thank_entities")
                                    if thank_entities:
                                        await client.send_message(entity=target_chat, message=thank_text, formatting_entities=thank_entities, link_preview=False)
                                    else:
                                        await client.send_message(entity=target_chat, message=thank_text, link_preview=False)
                                log.info("Sent thank-you message as user to %s", target_chat)
                            except Exception:
                                log.exception("Failed to send thank-you message from user")
                        else:
                            log.info("No mapping found for payload %s — it may have expired", invoice_payload)

        except asyncio.CancelledError:
            log.info("bot_updates_task cancelled")
            break
        except Exception:
            log.exception("Exception in bot_updates_task loop")
            await asyncio.sleep(2.0)


# ---- Outgoing handler (.info, .refund, .star) ----
async def outgoing_handler(event: events.NewMessage.Event):
    text = (event.raw_text or "").strip()
    if not text:
        return

    if text.lower().startswith(".info"):
        info_text = (
            "Команды:\n"
            ".star <сумма> — отправляет чек (текст + ссылка) пользователю.⭐\n"
            ".refund <user_id> <telegram_payment_charge_id> — (только админ) возвращает звёзды.\n\n"
            "При оплате чек удаляется и в том же чате от вас отправляется 'Спасибо за покупку!'."
        )
        sent = await event.reply(info_text)
        schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
        schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
        return

    if text.lower().startswith(".refund"):
        if ADMIN_ID is None or event.sender_id != ADMIN_ID:
            sent = await event.reply("Нет прав на выполнение .refund.")
            schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
            schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
            return
        parts = text.split()
        if len(parts) < 3:
            sent = await event.reply("Использование: .refund <user_id> <telegram_payment_charge_id>")
            schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
            schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
            return
        user_id = parts[1]
        payment_id = parts[2]
        sent = await event.reply("Выполняю возврат...")
        try:
            resp = await perform_refund(user_id=user_id, telegram_payment_charge_id=payment_id)
            if resp.get("ok"):
                done = await event.reply("✅ Звёзды успешно возвращены.")
                schedule_delete(done.chat_id, done.id, DELETION_DELAY)
            else:
                desc = resp.get("description") or json.dumps(resp, ensure_ascii=False)
                err = await event.reply(f"❌ Ошибка возврата: {desc}")
                schedule_delete(err.chat_id, err.id, DELETION_DELAY)
        except Exception:
            log.exception("Exception while performing refund")
            err = await event.reply("❌ Ошибка при попытке возврата (см логи).")
            schedule_delete(err.chat_id, err.id, DELETION_DELAY)
        schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
        schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
        return

    if text.lower().startswith(".star"):
        parts = text.split()
        if len(parts) == 2 and event.is_reply:
            amount_str = parts[1]
            replied = await event.get_reply_message()
            if not replied or not replied.sender_id:
                sent = await event.reply("Не удалось получить ID пользователя из reply.")
                schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
                schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
                return
            target_id = replied.sender_id
            user_chat_id_for_invoice = replied.chat_id
        elif len(parts) >= 3:
            target_spec = parts[1]
            amount_str = parts[2]
            try:
                if target_spec.startswith("@"):
                    ent = await client.get_entity(target_spec) if client is not None else None
                    target_id = getattr(ent, "id", None)
                    user_chat_id_for_invoice = getattr(ent, "id", None)
                else:
                    target_id = int(target_spec)
                    user_chat_id_for_invoice = target_id
            except Exception:
                target_id = None
                user_chat_id_for_invoice = None
            if target_id is None:
                sent = await event.reply("Не удалось разрешить цель. Укажите @username или user_id.")
                schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
                schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
                return
        else:
            sent = await event.reply("Использование: .star <сумма> (reply) или .star @username <сумма>")
            schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
            schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
            return

        # parse amount
        try:
            amount_f = float(amount_str.replace(",", "."))
            if amount_f <= 0:
                raise ValueError()
            amount = int(amount_f)
            if amount == 0:
                sent = await event.reply("Укажите целое количество звёзд (>=1).")
                schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
                schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
                return
        except Exception:
            sent = await event.reply("Неверный формат суммы. Пример: .star 5")
            schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
            schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
            return

        # Формируем чек
        title = f"Чек на {amount} звёзд"
        description = f"Описание: Оплата {amount} звёзд ({CURRENCY})."
        base_payload = f"user_invoice_{event.sender_id}"

        # create invoice link
        link_resp = await create_invoice_link_via_bot(title=title, description=description, amount=amount, base_payload=base_payload)
        if not link_resp.get("ok"):
            log.error("createInvoiceLink failed: %s", link_resp)
            sent = await event.reply("Не удалось создать ссылку для оплаты.")
            schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
            schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
            return

        result = link_resp.get("result", {})
        used_payload = result.get("used_payload", "<unknown>")
        invoice_url = None
        if isinstance(result, dict):
            invoice_url = result.get("url") or result.get("invoice_url") or result.get("payment_url")
            if not invoice_url and "_raw_result" in result:
                raw = result["_raw_result"]
                if isinstance(raw, str) and raw.startswith("http"):
                    invoice_url = raw

        if not invoice_url:
            short = {}
            if isinstance(result, dict) and "_raw_result" in result:
                short["_raw_result"] = result["_raw_result"]
            short["used_payload"] = used_payload
            sent = await event.reply(f"createInvoiceLink вернул неожиданный результат: {json.dumps(short, ensure_ascii=False)}")
            schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
            schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
            log.warning("createInvoiceLink result has no url: %s", result)
            return

        # Формируем текст и entities: "Оплата" — маскированная ссылка (без emoji в чеке)
        try:
            payment_text = "Оплата"
            message_text = f"{title}\n{description}\n{payment_text}"

            entities = []
            # find offset of payment_text
            offset = message_text.rfind(payment_text)
            if offset >= 0:
                # use positional args — более совместимо с разными версиями Telethon
                entities.append(MessageEntityTextUrl(offset, len(payment_text), invoice_url))

            # send message using formatting_entities (не entities) — это ожидаемый параметр в Telethon 1.42.0
            user_msg = await client.send_message(entity=target_id, message=message_text, formatting_entities=entities, link_preview=False)

            # Prepare thank-you text (with custom emoji appended only there)
            thank_base = "Спасибо за покупку!"
            if INVOICE_CUSTOM_EMOJI_ID:
                placeholder_char = "\uFFFC"
                thank_text = f"{thank_base} {placeholder_char}"
                # position of the placeholder char:
                emoji_offset = len(thank_base) + 1  # +1 for the space
                # positional args for MessageEntityCustomEmoji(offset, length, custom_emoji_id)
                thank_entities = [MessageEntityCustomEmoji(emoji_offset, 1, int(INVOICE_CUSTOM_EMOJI_ID))]
            else:
                thank_text = thank_base
                thank_entities = None

            # register mapping for deletion on successful payment and for thank-you
            await register_invoice(used_payload, {
                "type": "user",
                "bot_chat_id": None,
                "bot_msg_id": None,
                "user_chat_id": target_id,
                "user_msg_id": getattr(user_msg, "id", None),
                "initiator_id": event.sender_id,
                "thank_text": thank_text,
                "thank_entities": thank_entities,
            })

            # delete command message
            try:
                await client.delete_messages(event.chat_id, [event.message.id])
            except Exception:
                schedule_delete(event.chat_id, event.message.id, 1.0)
            return

        except UserIsBlockedError:
            sent = await event.reply("Не удалось отправить ссылку: пользователь заблокировал вас.")
            schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
            schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
        except PeerIdInvalidError:
            sent = await event.reply("Не удалось отправить ссылку: неверный peer (id).")
            schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
            schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
        except RpcCallFailError as e:
            sent = await event.reply(f"Ошибка при отправке сообщения (RPC): {e}")
            schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
            schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)
        except Exception as e:
            log.exception("Failed to send invoice URL to target")
            sent = await event.reply(f"Не удалось отправить ссылку пользователю: {e}")
            schedule_delete(event.chat_id, event.message.id, DELETION_DELAY)
            schedule_delete(sent.chat_id, sent.id, DELETION_DELAY)


# ------------------- Module lifecycle -----------------------
async def _start_bot_task():
    global _BOT_TASK
    if _BOT_TASK is None or _BOT_TASK.done():
        _BOT_TASK = asyncio.create_task(bot_updates_task())
        log.info("bot_updates_task started (module)")


async def _stop_bot_task():
    global _BOT_TASK
    if _BOT_TASK is not None:
        try:
            _BOT_TASK.cancel()
            await asyncio.wait_for(_BOT_TASK, timeout=5.0)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Error while stopping bot_updates_task")
        _BOT_TASK = None
        log.info("bot_updates_task stopped (module)")


async def _ensure_bot_token_and_start():
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN пустой — бот-поллинг не будет запущен.")
        return
    try:
        resp = await call_bot_api("getMe", {})
        if not resp.get("ok"):
            log.error("Bot API getMe failed: %s", resp)
            return
        me = resp.get("result")
        log.info("Bot token valid — bot username: %s (id=%s). Starting bot poller.", me.get("username"), me.get("id"))
        await _start_bot_task()
    except Exception:
        log.exception("Failed to validate BOT_TOKEN with getMe; bot-poller will not start.")


def setup(client_obj):
    global client, _REGISTERED_HANDLERS

    client = client_obj

    eb_outgoing = events.NewMessage(outgoing=True)
    client.add_event_handler(outgoing_handler, eb_outgoing)
    _REGISTERED_HANDLERS.append((outgoing_handler, eb_outgoing))

    try:
        if hasattr(client, "loop") and client.loop is not None:
            client.loop.create_task(_ensure_bot_token_and_start())
        else:
            asyncio.get_event_loop().create_task(_ensure_bot_token_and_start())
    except Exception:
        log.exception("Failed to start bot_updates_task in setup()")


async def teardown(client_obj):
    global client, _REGISTERED_HANDLERS

    try:
        await _stop_bot_task()
    except Exception:
        log.exception("Error stopping bot task in teardown")

    try:
        if client is not None:
            for func, ev in list(_REGISTERED_HANDLERS):
                try:
                    client.remove_event_handler(func, ev)
                except Exception:
                    log.exception("Failed to remove handler %s %s", func, ev)
        _REGISTERED_HANDLERS.clear()
    except Exception:
        log.exception("Error while removing handlers in teardown")

    client = None

    try:
        async with INVOICE_MAP_LOCK:
            INVOICE_MAP.clear()
    except Exception:
        pass
