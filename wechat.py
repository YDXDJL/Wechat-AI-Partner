"""Direct WeChat integration using wechat_clawbot API (no MCP server needed)."""

import asyncio
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass

from image_utils import ImageAttachment, detect_image_mime, extension_for_mime, load_image_attachment, MAX_IMAGE_BYTES
from wechat_clawbot.api.client import (
    WeixinApiOptions, get_updates, send_message, send_typing, get_config, close_shared_client,
)
from wechat_clawbot.api.types import (
    MessageItemType, MessageType, TypingStatus,
    WeixinMessage, SendTypingReq,
)
from wechat_clawbot.auth.accounts import CDN_BASE_URL
from wechat_clawbot.claude_channel.credentials import load_credentials
from wechat_clawbot.media.download import download_media_from_item
from wechat_clawbot.messaging.inbound import body_from_item_list, set_context_token
from wechat_clawbot.messaging.send import send_message_weixin

logger = logging.getLogger(__name__)


@dataclass
class WeChatMessage:
    sender_id: str
    sender: str
    content: str
    raw: WeixinMessage
    images: list[ImageAttachment] | None = None
    media_errors: list[str] | None = None


class WeChatClient:
    """Direct WeChat client that polls for messages and sends replies."""

    def __init__(self, base_dir: str = "."):
        self.base_dir = base_dir
        self._creds = None
        self._api_opts: WeixinApiOptions | None = None
        self._msg_queue: queue.Queue = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._connected = False
        self._context_tokens: dict[str, str] = {}  # sender_id -> context_token
        self._seen_msg_ids: set[int] = set()  # dedup by message_id
        self._seen_seqs: set[int] = set()  # dedup by seq
        self._recent_inbound: dict[tuple[str, str], float] = {}
        self._recent_outbound: dict[tuple[str, str], float] = {}

    def connect(self, account_data=None) -> bool:
        """Connect to WeChat. Accepts AccountData or falls back to default credentials."""
        if account_data:
            self._creds = account_data
        else:
            self._creds = load_credentials()
        if not self._creds:
            logger.warning("No WeChat credentials found")
            return False
        self._api_opts = WeixinApiOptions(
            base_url=self._creds.base_url,
            token=self._creds.token,
        )
        self._connected = True
        logger.info(f"WeChat client connected (account: {self._creds.account_id})")
        return True

    def start_polling(self):
        """Start background poll loop in a daemon thread."""
        if not self._connected:
            raise RuntimeError("WeChat client not connected. Call connect() first.")
        if self._running:
            return
        self._running = True
        self._seen_msg_ids.clear()
        self._seen_seqs.clear()
        self._recent_inbound.clear()
        self._recent_outbound.clear()
        self._drain_queue()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_poll_loop, daemon=True)
        self._thread.start()
        logger.info("WeChat polling started")

    def stop_polling(self):
        """Stop the background poll loop."""
        self._running = False
        if self._loop and not self._loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(close_shared_client(), self._loop)
            try:
                future.result(timeout=5)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
            self._loop = None
            self._thread = None
        logger.info("WeChat polling stopped")

    def get_message(self, block=False, timeout=0.5) -> WeChatMessage | None:
        """Non-blocking get of next incoming WeChat message."""
        try:
            return self._msg_queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

    def send_reply(self, sender_id: str, text: str):
        """Send a text reply to a WeChat user."""
        if not self._connected:
            logger.warning("WeChat not connected, cannot send reply")
            return
        if self._is_recent_outbound_duplicate(sender_id, text):
            logger.info(f"Skipped duplicate WeChat reply to {sender_id}: {text[:50]}...")
            return
        self._run_async(self._send_reply_async(sender_id, text))

    def send_file(self, sender_id: str, file_path: str):
        """Send a file to a WeChat user."""
        if not self._connected:
            return
        self._run_async(self._send_file_async(sender_id, file_path))

    def send_image(self, sender_id: str, image_path: str):
        """Send an image to a WeChat user."""
        if not self._connected:
            return
        self._run_async(self._send_image_async(sender_id, image_path))

    def start_typing(self, sender_id: str):
        """Show typing indicator for a user."""
        if not self._connected:
            return
        self._run_async(self._typing_async(sender_id, TypingStatus.TYPING))

    def stop_typing(self, sender_id: str):
        """Hide typing indicator for a user."""
        if not self._connected:
            return
        self._run_async(self._typing_async(sender_id, TypingStatus.CANCEL))

    # --- Internal async methods ---

    def _drain_queue(self):
        while True:
            try:
                self._msg_queue.get_nowait()
            except queue.Empty:
                break

    def _prune_recent(self, store: dict, now: float, ttl: float) -> None:
        for key, seen_at in list(store.items()):
            if now - seen_at > ttl:
                store.pop(key, None)

    def _is_recent_inbound_duplicate(self, sender_id: str, content: str, ttl: float = 4.0) -> bool:
        now = time.monotonic()
        self._prune_recent(self._recent_inbound, now, ttl)
        key = (sender_id, content)
        last_seen = self._recent_inbound.get(key)
        self._recent_inbound[key] = now
        return last_seen is not None and now - last_seen <= ttl

    def _media_dir(self, sender_id: str) -> str:
        account_id = self._creds.account_id if self._creds else "unknown-account"
        safe_account = "".join(c if c.isalnum() or c in ".@_-" else "_" for c in account_id)
        safe_sender = "".join(c if c.isalnum() or c in ".@_-" else "_" for c in sender_id or "unknown")
        path = os.path.join(self.base_dir, "media", "wechat", safe_account, safe_sender)
        os.makedirs(path, exist_ok=True)
        return path

    async def _save_inbound_media(self, buf: bytes, mime: str | None, _direction: str, max_bytes: int, file_name: str | None = None):
        if len(buf) > min(max_bytes, MAX_IMAGE_BYTES):
            raise ValueError(f"media is too large: {len(buf)} bytes")
        mime_type = mime or detect_image_mime(buf)
        ext = extension_for_mime(mime_type or "")
        name = file_name or f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}{ext}"
        if not os.path.splitext(name)[1]:
            name += ext
        path = os.path.join(self._current_media_dir, os.path.basename(name))
        with open(path, "wb") as f:
            f.write(buf)
        return {"path": path, "mime": mime_type or "application/octet-stream"}

    async def _download_images(self, msg: WeixinMessage, sender_id: str) -> tuple[list[ImageAttachment], list[str]]:
        images: list[ImageAttachment] = []
        errors: list[str] = []
        items = msg.item_list or []
        for index, item in enumerate(items):
            if item.type != MessageItemType.IMAGE:
                continue
            try:
                self._current_media_dir = self._media_dir(sender_id)
                opts = await download_media_from_item(
                    item,
                    CDN_BASE_URL,
                    self._save_inbound_media,
                    lambda text: logger.debug(text),
                    lambda text: logger.warning(text),
                    f"{sender_id}:{msg.message_id or msg.seq or index}",
                )
                if opts.decrypted_pic_path:
                    message_id = str(item.msg_id or msg.message_id or msg.seq or index)
                    images.append(load_image_attachment(
                        opts.decrypted_pic_path,
                        source="wechat",
                        message_id=message_id,
                    ))
            except Exception as e:
                errors.append(str(e))
                logger.warning(f"Failed to download WeChat image from {sender_id}: {e}")
        return images, errors

    def _is_recent_outbound_duplicate(self, sender_id: str, text: str, ttl: float = 8.0) -> bool:
        now = time.monotonic()
        self._prune_recent(self._recent_outbound, now, ttl)
        key = (sender_id, text)
        last_sent = self._recent_outbound.get(key)
        self._recent_outbound[key] = now
        return last_sent is not None and now - last_sent <= ttl

    def _run_async(self, coro):
        """Run an async coroutine on the background event loop."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._thread.start()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)

    async def _send_reply_async(self, sender_id: str, text: str):
        # Send directly to the user id. Some iLink sessions duplicate delivery when
        # both to_user_id and context_token are present in the same sendmessage call.
        opts = WeixinApiOptions(
            base_url=self._api_opts.base_url,
            token=self._api_opts.token,
            context_token=None,
        )
        await send_message_weixin(to=sender_id, text=text, opts=opts)

    async def _send_file_async(self, sender_id: str, file_path: str):
        from wechat_clawbot.cdn.upload import upload_file_attachment_to_weixin
        from wechat_clawbot.messaging.send import send_file_message_weixin
        uploaded = await upload_file_attachment_to_weixin(file_path, sender_id, self._api_opts, CDN_BASE_URL)
        await send_file_message_weixin(sender_id, "", file_path, uploaded, self._api_opts)

    async def _send_image_async(self, sender_id: str, image_path: str):
        from wechat_clawbot.cdn.upload import upload_file_to_weixin
        from wechat_clawbot.messaging.send import send_image_message_weixin
        uploaded = await upload_file_to_weixin(image_path, sender_id, self._api_opts, CDN_BASE_URL)
        await send_image_message_weixin(sender_id, "", uploaded, self._api_opts)

    async def _typing_async(self, sender_id: str, status: TypingStatus):
        account_id = self._creds.account_id if self._creds else ""
        try:
            config = await get_config(self._api_opts, sender_id)
            await send_typing(self._api_opts, SendTypingReq(
                ilink_user_id=sender_id,
                typing_ticket=config.typing_ticket,
                status=status,
            ))
        except Exception as e:
            logger.warning(f"Failed to set typing status: {e}")

    def _run_poll_loop(self):
        """Run the async poll loop on the background event loop."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._poll_loop())

    async def _poll_loop(self):
        """Long-poll loop that fetches new messages and puts them in the queue."""
        sync_buf = ""
        consecutive_errors = 0
        while self._running:
            try:
                if self._creds is None:
                    break
                resp = await get_updates(
                    base_url=self._creds.base_url,
                    token=self._creds.token,
                    get_updates_buf=sync_buf,
                )
                consecutive_errors = 0

                if resp.get_updates_buf:
                    sync_buf = resp.get_updates_buf

                if not resp.msgs:
                    continue

                for msg in resp.msgs:
                    # Only process user messages
                    if msg.message_type != MessageType.USER:
                        continue

                    # Dedup by message_id or seq
                    msg_key = msg.message_id or msg.seq
                    if msg_key is not None:
                        if msg.message_id and msg.message_id in self._seen_msg_ids:
                            continue
                        if msg.seq and msg.seq in self._seen_seqs:
                            continue
                        if msg.message_id:
                            self._seen_msg_ids.add(msg.message_id)
                        if msg.seq:
                            self._seen_seqs.add(msg.seq)
                        # Keep sets bounded
                        if len(self._seen_msg_ids) > 1000:
                            self._seen_msg_ids = set(list(self._seen_msg_ids)[-500:])
                        if len(self._seen_seqs) > 1000:
                            self._seen_seqs = set(list(self._seen_seqs)[-500:])

                    sender_id = msg.from_user_id or ""
                    content = body_from_item_list(msg.item_list)
                    images, media_errors = await self._download_images(msg, sender_id)
                    has_image_item = any(item.type == MessageItemType.IMAGE for item in (msg.item_list or []))
                    if not content and not images and not (has_image_item and media_errors):
                        continue
                    dedupe_content = content or "|".join(i.path for i in images) or f"image-error:{msg.message_id or msg.seq}:{media_errors}"
                    if self._is_recent_inbound_duplicate(sender_id, dedupe_content):
                        logger.info(f"Skipped duplicate WeChat inbound from {sender_id}: {content[:50]}...")
                        continue

                    # Update context token
                    if msg.context_token:
                        set_context_token(
                            self._creds.account_id,
                            sender_id,
                            msg.context_token,
                        )
                        self._context_tokens[sender_id] = msg.context_token

                    wechat_msg = WeChatMessage(
                        sender_id=sender_id,
                        sender=sender_id or "unknown",
                        content=content,
                        raw=msg,
                        images=images,
                        media_errors=media_errors,
                    )
                    self._msg_queue.put_nowait(wechat_msg)
                    logger.debug(f"WeChat message queued from {wechat_msg.sender}: {content[:50]}...")

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.warning(f"WeChat poll error ({consecutive_errors}): {e}")
                if consecutive_errors >= 3:
                    logger.error("Too many consecutive errors, pausing poll for 30s")
                    await asyncio.sleep(30)
                    consecutive_errors = 0
                else:
                    await asyncio.sleep(2)
