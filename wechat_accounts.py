"""WeChat multi-account manager using wechat_clawbot auth module."""

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from wechat_clawbot.auth.accounts import (
    list_weixin_account_ids,
    load_weixin_account,
    save_weixin_account,
    unregister_weixin_account_id,
    normalize_account_id,
)
from wechat_clawbot.auth.login_qr import (
    start_weixin_login_with_qr,
    wait_for_weixin_login,
)
from wechat_clawbot.claude_channel.credentials import AccountData, load_credentials

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


@dataclass
class AccountInfo:
    account_id: str
    user_id: str | None
    saved_at: str | None
    base_url: str | None
    skill: str | None = None  # bound skill name


class WeChatAccountManager:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._migrate_old_account()

    def _ensure_loop(self):
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run_async(self, coro):
        loop = self._ensure_loop()
        return loop.run_until_complete(coro)

    def _migrate_old_account(self):
        """Migrate account from old single-account location if exists."""
        existing = list_weixin_account_ids()
        if existing:
            return  # Already has accounts in new system

        old_creds = load_credentials()
        if old_creds and old_creds.account_id:
            logger.info(f"Migrating old account: {old_creds.account_id}")
            save_weixin_account(
                account_id=old_creds.account_id,
                token=old_creds.token,
                base_url=old_creds.base_url,
                user_id=old_creds.user_id,
            )

    def list_accounts(self) -> list[AccountInfo]:
        """List all saved WeChat accounts."""
        account_ids = list_weixin_account_ids()
        bindings = self._load_skill_bindings()
        accounts = []
        for aid in account_ids:
            data = load_weixin_account(aid)
            skill = bindings.get(aid)
            if data:
                accounts.append(AccountInfo(
                    account_id=aid,
                    user_id=data.user_id,
                    saved_at=data.saved_at,
                    base_url=data.base_url,
                    skill=skill,
                ))
            else:
                accounts.append(AccountInfo(
                    account_id=aid,
                    user_id=None,
                    saved_at=None,
                    base_url=None,
                    skill=skill,
                ))
        return accounts

    def get_account(self, account_id: str) -> AccountData | None:
        """Load account credentials as AccountData for WeChatClient."""
        data = load_weixin_account(account_id)
        if not data or not data.token:
            return None
        return AccountData(
            token=data.token,
            base_url=data.base_url or DEFAULT_BASE_URL,
            account_id=account_id,
            user_id=data.user_id,
            saved_at=data.saved_at,
        )

    def get_default_account(self) -> tuple[AccountData | None, str | None]:
        """Load the last selected account, falling back to the first saved account."""
        account_id = self.get_last_account_id()
        if account_id:
            account = self.get_account(account_id)
            if account:
                return account, account_id

        accounts = self.list_accounts()
        if not accounts:
            return None, None

        account_id = accounts[0].account_id
        account = self.get_account(account_id)
        if account:
            self.set_last_account_id(account_id)
            return account, account_id
        return None, None

    def qr_login(self) -> AccountData | None:
        """Run QR code login flow. Returns AccountData on success, None on failure."""
        print("\n正在获取微信扫码登录二维码...")

        result = self._run_async(
            start_weixin_login_with_qr(api_base_url=DEFAULT_BASE_URL)
        )

        if not result.qrcode_url:
            print(f"获取二维码失败: {result.message}")
            return None

        print(f"\n请用微信扫描以下链接打开的二维码：")
        print(f"  {result.qrcode_url}")
        print(f"\n或在浏览器中打开此链接扫码。")
        print(f"等待扫码中", end="", flush=True)

        try:
            import webbrowser
            webbrowser.open(result.qrcode_url)
            print(" (已在浏览器中打开)")
        except Exception:
            print()

        wait_result = self._run_async(
            wait_for_weixin_login(
                session_key=result.session_key,
                api_base_url=DEFAULT_BASE_URL,
                verbose=True,
            )
        )

        print()  # newline after dots

        if not wait_result.connected:
            print(f"登录失败: {wait_result.message}")
            return None

        # Save the account
        account_id = wait_result.account_id
        save_weixin_account(
            account_id=account_id,
            token=wait_result.bot_token,
            base_url=wait_result.base_url,
            user_id=wait_result.user_id,
        )

        print(f"登录成功! 账号: {account_id}")
        self.set_last_account_id(account_id)

        return AccountData(
            token=wait_result.bot_token,
            base_url=wait_result.base_url or DEFAULT_BASE_URL,
            account_id=account_id,
            user_id=wait_result.user_id,
        )

    def remove_account(self, account_id: str) -> bool:
        """Remove an account. Returns True if successful."""
        try:
            unregister_weixin_account_id(account_id)
            return True
        except Exception as e:
            logger.warning(f"Failed to remove account {account_id}: {e}")
            return False

    def _bindings_path(self) -> str:
        base = os.path.expanduser("~/.openclaw/openclaw-weixin")
        return os.path.join(base, "skill-bindings.json")

    def _state_path(self) -> str:
        base = os.path.expanduser("~/.openclaw/openclaw-weixin")
        return os.path.join(base, "agent-state.json")

    def get_last_account_id(self) -> str | None:
        """Return the account id selected in the last run."""
        path = self._state_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            account_id = data.get("last_account_id")
            return account_id if isinstance(account_id, str) and account_id else None
        except Exception:
            return None

    def set_last_account_id(self, account_id: str):
        """Persist the account id to use by default next time."""
        path = self._state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"last_account_id": account_id}, f, indent=2, ensure_ascii=False)

    def _load_skill_bindings(self) -> dict[str, str]:
        """Load skill bindings: {account_id: skill_name}."""
        path = self._bindings_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_skill_bindings(self, bindings: dict[str, str]):
        """Save skill bindings."""
        path = self._bindings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bindings, f, indent=2, ensure_ascii=False)

    def bind_skill(self, account_id: str, skill_name: str):
        """Bind a skill to an account."""
        bindings = self._load_skill_bindings()
        bindings[account_id] = skill_name
        self._save_skill_bindings(bindings)
        logger.info(f"Bound skill '{skill_name}' to account '{account_id}'")

    def unbind_skill(self, account_id: str):
        """Remove skill binding from an account."""
        bindings = self._load_skill_bindings()
        bindings.pop(account_id, None)
        self._save_skill_bindings(bindings)

    def get_bound_skill(self, account_id: str) -> str | None:
        """Get the skill bound to an account."""
        bindings = self._load_skill_bindings()
        return bindings.get(account_id)
