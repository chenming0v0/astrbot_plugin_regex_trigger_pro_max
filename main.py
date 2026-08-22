import asyncio
import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

# 唤醒来源标记，存放在 event extra 中，供后续分析阶段判断
WAKE_SOURCE_KEY = "rtpm_wake_source"
EMOTION_KEY = "rtpm_emotion_data"

SOURCE_REGEX = "regex"          # 正则唤醒词命中
SOURCE_CONTINUOUS = "continuous"  # 持续唤醒窗口内
SOURCE_NATIVE = "native"        # AstrBot 原生唤醒（@ / 唤醒前缀 / 指令）


@register(
    "astrbot_plugin_regex_trigger_pro_max",
    "辰林 & 小辰",
    "正则唤醒 + 持续唤醒 + 小模型二次判定，融合版触发插件 Pro Max",
    "v1.0.2",
    "https://github.com/idk114-514/should_I_respond",
)
class RegexTriggerProMax(Star):
    """把 wake_enhance 的正则/持续唤醒与 should_I_respond 的小模型判定串成一条流程。

    流程顺序：
        1. 消息进来 -> 正则唤醒词 / 持续唤醒窗口判定，命中则置 is_at_or_wake_command
        2. 记录唤醒来源（regex / continuous / native）
        3. LLM 请求前 -> 按配置决定是否让小模型做「该不该回」判定
        4. 决定回复 -> 把 interest / feeling 注入 prompt
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # ---------- 唤醒相关 ----------
        self.waking_regex: List[str] = config.get("waking_regex", []) or []
        self.c_awake: Dict[str, Any] = config.get("continuous_awakening", {}) or {}
        self.whitelist: List[str] = config.get("whitelist", []) or []
        self.waking_sessions: Dict[str, Dict[str, float]] = {}
        self._compiled_regex: List[re.Pattern] = []
        self._compile_regex()

        # ---------- 分析相关 ----------
        self.history_cache: Dict[str, list] = {}
        self.history_file = Path("data") / "rtpm_interest_history.json"
        self.history_lock = asyncio.Lock()

        asyncio.create_task(self._load_history())
        logger.info("[RTPM] 正则触发插件 Pro Max 已加载。")

    # ==================================================================
    # 唤醒段：来自 wake_enhance
    # ==================================================================

    def _compile_regex(self):
        """预编译唤醒正则，坏表达式直接跳过而不是整条流程炸掉。"""
        self._compiled_regex = []
        for pattern in self.waking_regex:
            try:
                self._compiled_regex.append(re.compile(pattern))
            except re.error as e:
                logger.error(f"[RTPM] 唤醒正则编译失败，已跳过：{pattern} ({e})")

    def _in_whitelist(self, umo: str) -> bool:
        """空白名单代表不限制。统一用 UMO 判定，群聊私聊一套逻辑。"""
        if not self.whitelist:
            return True
        return str(umo) in [str(x) for x in self.whitelist]

    def _match_regex(self, message_str: str) -> bool:
        if not message_str:
            return False
        for pattern in self._compiled_regex:
            if pattern.search(message_str):
                return True
        return False

    def _touch_continuous(self, umo: str):
        self.waking_sessions[umo] = {"last_time": time.time()}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def wake_listener(self, event: AstrMessageEvent):
        """唤醒判定入口，并把唤醒来源写进 event extra。"""
        # 原生唤醒（@、唤醒前缀、指令）先记一笔，后面分析阶段要用
        if event.is_at_or_wake_command:
            event.set_extra(WAKE_SOURCE_KEY, SOURCE_NATIVE)

        if event.is_private_chat():
            return

        umo = event.unified_msg_origin
        if not self._in_whitelist(umo):
            return

        # 1. 正则唤醒
        if self._match_regex(event.message_str):
            if not event.is_at_or_wake_command:
                event.set_extra(WAKE_SOURCE_KEY, SOURCE_REGEX)
            event.is_at_or_wake_command = True
            if self.c_awake.get("enable", False):
                self._touch_continuous(umo)
            return

        # 2. 持续唤醒窗口
        if umo not in self.waking_sessions:
            return

        interval = float(self.c_awake.get("waking_interval", 30))
        if time.time() - self.waking_sessions[umo]["last_time"] > interval:
            self.waking_sessions.pop(umo, None)
            logger.info(f"[RTPM] 会话 {umo} 持续唤醒超时退出。")
            return

        if not event.is_at_or_wake_command:
            event.set_extra(WAKE_SOURCE_KEY, SOURCE_CONTINUOUS)
        event.is_at_or_wake_command = True

        if self.c_awake.get("reset_when_reply", False):
            self._touch_continuous(umo)

    # ==================================================================
    # 分析段：来自 should_I_respond
    # ==================================================================

    async def _load_history(self):
        async with self.history_lock:
            if self.history_file.exists():
                try:
                    async with aiofiles.open(self.history_file, "r", encoding="utf-8") as f:
                        self.history_cache = json.loads(await f.read())
                    logger.info(f"[RTPM] 已载入历史记录：{self.history_file}")
                except Exception as e:
                    logger.error(f"[RTPM] 历史记录载入失败：{e}")

    async def _save_history(self):
        async with self.history_lock:
            try:
                max_len = self.config.get("history_max_length", 20)
                for session_id in self.history_cache:
                    self.history_cache[session_id] = self.history_cache[session_id][-max_len:]
                self.history_file.parent.mkdir(parents=True, exist_ok=True)
                async with aiofiles.open(self.history_file, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(self.history_cache, indent=2, ensure_ascii=False))
            except Exception as e:
                logger.error(f"[RTPM] 历史记录保存失败：{e}")

    async def _get_current_persona_prompt(self, event: AstrMessageEvent) -> str:
        try:
            uid = event.unified_msg_origin
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            if not curr_cid:
                return ""
            conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
            if not conversation:
                return ""
            persona_id = conversation.persona_id
            if persona_id and persona_id != "[%None]":
                target_persona_name = persona_id
            elif not persona_id:
                target_persona_name = self.context.provider_manager.selected_default_persona.get("name")
            else:
                return ""
            if not target_persona_name:
                return ""
            for persona_dict in self.context.provider_manager.personas:
                if persona_dict.get("name") == target_persona_name:
                    return persona_dict.get("prompt", "")
            return ""
        except Exception:
            return ""

    def _should_analyze(self, event: AstrMessageEvent) -> bool:
        """决定这一条要不要交给小模型判定。

        不管是原生 @、唤醒前缀，还是正则唤醒 / 持续唤醒这类软唤醒，
        只要在白名单里就一律走一遍小模型，保证情绪判定不被绕过。
        """
        return self._in_whitelist(event.unified_msg_origin)

    async def _get_analysis_provider(self, event: AstrMessageEvent):
        """拿判定用的供应商：优先配置里指定的小模型，没配或失效时回退到当前会话/默认供应商。

        回退而不是放弃，否则正则软唤醒的消息会在没配置时全部直通主模型，
        判定环节等于不存在。
        """
        provider_id = self.config.get("analysis_provider_id")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider:
                return provider
            logger.warning(
                f"[RTPM] 配置的判定供应商 {provider_id} 不存在，回退到当前默认供应商。"
            )
        try:
            # 新版 AstrBot 是 get_using_provider_async，旧版只有同步的 get_using_provider
            get_async = getattr(self.context, "get_using_provider_async", None)
            if get_async is not None:
                return await get_async(event.unified_msg_origin)
            return self.context.get_using_provider(event.unified_msg_origin)
        except Exception:
            return None

    async def _handle_analysis_fail(self, event: AstrMessageEvent, reason: str):
        """判定环节挂掉的统一出口：按 fail_policy 决定放行还是拦下，不再无条件放行。"""
        if self.config.get("analysis_fail_policy", "allow") == "block":
            logger.warning(f"[RTPM] {reason}，fail_policy=block，拦下本条不回复。")
            event.stop_event()
        else:
            logger.warning(f"[RTPM] {reason}，fail_policy=allow，放行。")

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """尽量从模型输出里抠出 JSON：直接解析 → 剥掉 markdown 代码围栏 → 正则兜底。

        便宜的小模型经常在 JSON 前后加「好的，以下是结果」或 ``` 围栏，
        原来的单正则抓不到就整个放行，这是「配了裁判还是看到就回」的主因之一。
        """
        text = (text or "").strip()
        if not text:
            return None
        candidates = [text]
        stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        if stripped != text:
            candidates.append(stripped)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            candidates.append(m.group(0))
        for cand in candidates:
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _format_history_entry(msg: dict) -> str:
        if msg.get("role") == "user":
            return (
                f"user ({msg.get('sender_name', 'unknown')}/"
                f"{msg.get('sender_id', '0')}): {msg.get('content')}"
            )
        return f"assistant: {msg.get('content')}"

    @filter.on_llm_request(priority=10)
    async def interest_analyzer(self, event: AstrMessageEvent, req: ProviderRequest):
        session_id = event.unified_msg_origin
        source = event.get_extra(WAKE_SOURCE_KEY) or SOURCE_NATIVE

        # 无论是否分析，都要把这条用户消息记进自管历史
        current_message = req.prompt or "User sent an empty or non-text message."
        prefix = "[Direct Mention] " if source == SOURCE_NATIVE else ""
        user_entry = {
            "role": "user",
            "sender_id": event.get_sender_id(),
            "sender_name": event.get_sender_name(),
            "content": f"{prefix}{current_message}",
            "wake_source": source,
        }
        self.history_cache.setdefault(session_id, []).append(user_entry)

        if not self._should_analyze(event):
            await self._save_history()
            return

        provider = await self._get_analysis_provider(event)
        if not provider:
            logger.warning("[RTPM] 没有任何可用的 LLM 供应商跑判定，本条按放行处理。")
            await self._save_history()
            return

        persona_description = await self._get_current_persona_prompt(event) or req.system_prompt or ""
        history_for_analysis = self.history_cache[session_id][:-1]
        formatted_history = (
            "\n".join(map(self._format_history_entry, history_for_analysis))
            or "No previous conversation history."
        )

        wake_context_map = {
            SOURCE_REGEX: "本条消息命中了唤醒词正则，属于软唤醒，不一定是在直接跟你说话，请谨慎判断。",
            SOURCE_CONTINUOUS: "本条消息处于持续唤醒窗口内，可能只是群友之间在聊天，请谨慎判断。",
            SOURCE_NATIVE: "本条消息直接点名了你，通常应当回复。",
        }
        awakening_context_str = wake_context_map.get(source, "")

        try:
            template = self.config.get("analysis_system_prompt") or ""
            analysis_prompt = (
                template.replace("{awakening_context}", awakening_context_str)
                .replace("{persona}", persona_description)
                .replace("{history}", formatted_history)
                .replace("{current_message}", self._format_history_entry(user_entry))
            )

            provider_desc = (
                getattr(getattr(provider, "provider_config", None), "id", "")
                or type(provider).__name__
            )
            logger.info(f"[RTPM] 开始判定（来源 {source}，供应商 {provider_desc}）")

            resp = await provider.text_chat(prompt=analysis_prompt)
            response_text = resp.completion_text or ""
            logger.info(f"[RTPM] 判定模型原始回复：{response_text}")

            result = self._extract_json(response_text)
            if result is None:
                await self._handle_analysis_fail(event, f"分析模型没吐出可解析的 JSON：{response_text[:200]}")
                return

            if not result.get("should_reply", True):
                logger.info(f"[RTPM] 判定不回复（来源 {source}）：{result.get('reason')}")
                event.stop_event()
                await self._save_history()
                return

            chance = float(self.config.get("random_reply_chance", 1.0))
            if random.random() > chance:
                logger.info(f"[RTPM] 判定回复但随机检定未通过（{chance * 100:.0f}%），拦下。")
                event.stop_event()
                await self._save_history()
                return

            interest = result.get("interest", "normal")
            feeling = result.get("feeling", "neutral")
            event.set_extra(EMOTION_KEY, {"interest": interest, "feeling": feeling})

            if self.config.get("inject_emotion", True):
                req.prompt = (
                    f'User\'s message is: "{current_message}"\n\n'
                    f"[[System Note: Your current state is - Interest: '{interest}', "
                    f"Feeling: '{feeling}'. You MUST respond according to this state.]]"
                )
                logger.info(f"[RTPM] 已注入情绪状态：{interest} / {feeling}")

        except Exception as e:
            logger.error(f"[RTPM] 分析过程出错：{e}", exc_info=True)
            await self._handle_analysis_fail(event, f"分析模型调用出错：{e}")
        finally:
            await self._save_history()

    @filter.on_llm_response(priority=10)
    async def save_llm_reply_to_history(self, event: AstrMessageEvent, resp: LLMResponse):
        session_id = event.unified_msg_origin
        if session_id not in self.history_cache:
            return
        if not resp.completion_text:
            return

        entry = {"role": "assistant", "content": resp.completion_text}
        if self.config.get("record_emotion_in_history", False):
            emotion = event.get_extra(EMOTION_KEY)
            if emotion:
                entry["state"] = emotion

        self.history_cache.setdefault(session_id, []).append(entry)
        await self._save_history()

    async def terminate(self):
        await self._save_history()
        logger.info("[RTPM] 正则触发插件 Pro Max 已卸载。")
