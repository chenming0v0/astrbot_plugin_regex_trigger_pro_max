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

PLUGIN_ID = "astrbot_plugin_regex_trigger_pro_max"
PLUGIN_VERSION = "v1.4.2"

try:
    from astrbot.api import web as astrbot_web
except Exception:  # 旧版 AstrBot 没有插件页面 API，WebUI 整体不启用
    astrbot_web = None  # type: ignore[assignment]


@register(
    PLUGIN_ID,
    "辰林 & 小辰",
    "Bot唤醒Pro Max：正则唤醒 + 持续唤醒 + 小模型二次判定，自带 WebUI 控制台",
    PLUGIN_VERSION,
    "https://github.com/chenming0v0/astrbot_plugin_regex_trigger_pro_max",
)
class RegexTriggerProMax(Star):
    """把 wake_enhance 的正则/持续唤醒与 should_I_respond 的小模型判定串成一条流程。

    流程顺序：
        1. 消息进来 -> 正则唤醒词 / 持续唤醒窗口判定，命中则置 is_at_or_wake_command
        2. 记录唤醒来源（regex / continuous / native）
        3. LLM 请求前 -> 按配置决定是否让小模型做「该不该回」判定
        4. 决定回复 -> 把 interest / feeling 注入 prompt
    """

    # v1.2 及以前的平铺配置键 -> v1.3 分组结构
    LEGACY_KEYS = {
        "analysis_provider_id": ("analysis", "provider_id"),
        "analysis_fail_policy": ("analysis", "fail_policy"),
        "random_reply_chance": ("analysis", "random_reply_chance"),
        "inject_emotion": ("analysis", "inject_emotion"),
        "analysis_system_prompt": ("analysis", "system_prompt"),
        "whitelist": ("session", "whitelist"),
        "history_max_length": ("session", "history_max_length"),
        "record_emotion_in_history": ("session", "record_emotion_in_history"),
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._migrate_config()

        # ---------- 唤醒相关 ----------
        self.waking_regex: List[str] = config.get("waking_regex", []) or []
        self.c_awake: Dict[str, Any] = config.get("continuous_awakening", {}) or {}
        self.whitelist: List[str] = self._session_cfg().get("whitelist", []) or []
        self.waking_sessions: Dict[str, Dict[str, float]] = {}
        self._compiled_regex: List[re.Pattern] = []
        self._compile_regex()

        # ---------- 分析相关 ----------
        self.history_cache: Dict[str, list] = {}
        self.history_file = Path("data") / "rtpm_interest_history.json"
        self.history_lock = asyncio.Lock()

        asyncio.create_task(self._load_history())

        # ---------- WebUI 控制台 ----------
        self._schema_cache: Dict[str, Any] = self._load_schema()
        self._register_webui()

        logger.info("[RTPM] Bot唤醒Pro Max 已加载。")

    # ==================================================================
    # 唤醒段：来自 wake_enhance
    # ==================================================================

    def _analysis_cfg(self) -> Dict[str, Any]:
        return self.config.get("analysis", {}) or {}

    def _session_cfg(self) -> Dict[str, Any]:
        return self.config.get("session", {}) or {}

    def _migrate_config(self):
        """把 v1.2 及以前的平铺配置键迁移到 v1.3 的分组结构，只需跑一次。"""
        moved = False
        for old_key, (group, sub) in self.LEGACY_KEYS.items():
            if old_key in self.config:
                self.config.setdefault(group, {})
                self.config[group][sub] = self.config[old_key]
                del self.config[old_key]
                moved = True
        if not moved:
            return
        try:
            self.config.save_config()
            logger.info("[RTPM] 旧版平铺配置已自动迁移为分组结构。")
        except Exception as e:
            logger.warning(f"[RTPM] 配置迁移完成但落盘失败：{e}")

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
        provider_id = self._analysis_cfg().get("provider_id")
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
        if self._analysis_cfg().get("fail_policy", "allow") == "block":
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

        # 三种唤醒来源写入 {awakening_context} 的文案，可在配置里自定义
        wake_defaults = {
            SOURCE_REGEX: "本条消息命中了唤醒词正则，属于软唤醒，不一定是在直接跟你说话，请谨慎判断。",
            SOURCE_CONTINUOUS: "本条消息处于持续唤醒窗口内，可能只是群友之间在聊天，请谨慎判断。",
            SOURCE_NATIVE: "本条消息直接点名了你，通常应当回复。",
        }
        analysis_cfg = self._analysis_cfg()
        wake_context_map = {
            SOURCE_REGEX: analysis_cfg.get("wake_context_regex") or wake_defaults[SOURCE_REGEX],
            SOURCE_CONTINUOUS: analysis_cfg.get("wake_context_continuous") or wake_defaults[SOURCE_CONTINUOUS],
            SOURCE_NATIVE: analysis_cfg.get("wake_context_native") or wake_defaults[SOURCE_NATIVE],
        }
        awakening_context_str = wake_context_map.get(source, "")

        try:
            template = self._analysis_cfg().get("system_prompt") or ""
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

            # 判定模型发现自己没被注意，主动退出持续唤醒窗口。
            # 本条回不回仍由 should_reply 决定，允许它「回完最后一句再退场」。
            if result.get("exit_wake"):
                self.waking_sessions.pop(session_id, None)
                logger.info(f"[RTPM] 判定模型主动退出持续唤醒：{result.get('reason')}")

            if not result.get("should_reply", True):
                logger.info(f"[RTPM] 判定不回复（来源 {source}）：{result.get('reason')}")
                event.stop_event()
                await self._save_history()
                return

            chance = float(self._analysis_cfg().get("random_reply_chance", 1.0))
            if random.random() > chance:
                logger.info(f"[RTPM] 判定回复但随机检定未通过（{chance * 100:.0f}%），拦下。")
                event.stop_event()
                await self._save_history()
                return

            interest = result.get("interest", "normal")
            feeling = result.get("feeling", "neutral")
            event.set_extra(EMOTION_KEY, {"interest": interest, "feeling": feeling})

            if self._analysis_cfg().get("inject_emotion", True):
                req.prompt = (
                    f'User\'s message is: "{current_message}"\n\n'
                    f"[[System Note: Your current state is - Interest: '{interest}', "
                    f"Feeling: '{feeling}'. You MUST respond according to this state.]]"
                )
                logger.info(f"[RTPM] 已注入情绪状态：{interest} / {feeling}")

            # 退场提示：判定要退出唤醒且本条要回复，引导主模型把回复说成告别收尾
            exit_hint = analysis_cfg.get("exit_wake_hint") or ""
            if result.get("exit_wake") and exit_hint:
                req.prompt = f"{req.prompt or ''}\n\n" + exit_hint.replace(
                    "{reason}", str(result.get("reason", ""))
                )
                logger.info("[RTPM] 已注入退场提示。")

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
        if self._session_cfg().get("record_emotion_in_history", False):
            emotion = event.get_extra(EMOTION_KEY)
            if emotion:
                entry["state"] = emotion

        self.history_cache.setdefault(session_id, []).append(entry)
        await self._save_history()

    # ==================================================================
    # WebUI 段：配置控制台（接入方式参考 AstrNa 的插件页面机制）
    # ==================================================================

    def _load_schema(self) -> Dict[str, Any]:
        try:
            with open(Path(__file__).parent / "_conf_schema.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[RTPM] WebUI 读取配置模板失败：{e}")
            return {}

    def _register_webui(self):
        register = getattr(self.context, "register_web_api", None)
        if not callable(register) or astrbot_web is None:
            logger.info("[RTPM] 当前 AstrBot 不支持插件页面，WebUI 未启用（不影响其他功能）。")
            return
        base = f"/{PLUGIN_ID}/webui"
        try:
            register(f"{base}/config", self._webui_get_config, ["GET"], "Bot唤醒Pro Max 读取配置")
            register(f"{base}/config", self._webui_save_config, ["POST"], "Bot唤醒Pro Max 保存配置")
            register(f"{base}/status", self._webui_get_status, ["GET"], "Bot唤醒Pro Max 运行状态")
            register(f"{base}/session", self._webui_drop_session, ["POST"], "Bot唤醒Pro Max 退出唤醒")
            logger.info("[RTPM] WebUI 控制台已注册。")
        except Exception as e:
            logger.warning(f"[RTPM] WebUI 注册失败：{e}")

    async def _webui_get_config(self):
        providers = []
        try:
            # AstrBot 新版 Provider 用 meta() 暴露 id/model（同 AstrNa 的取法），旧版退回属性直取
            for p in self.context.get_all_providers():
                pid = ""
                label = ""
                meta_getter = getattr(p, "meta", None)
                meta = meta_getter() if callable(meta_getter) else None
                if meta is not None:
                    pid = getattr(meta, "id", "") or ""
                    model = str(getattr(meta, "model", "") or "").strip()
                    label = f"{pid}（{model}）" if model and model != pid else pid
                if not pid:
                    pid = getattr(getattr(p, "provider_config", None), "id", "") or getattr(p, "id", "") or ""
                    label = pid
                if pid:
                    providers.append({"id": pid, "label": label})
        except Exception:
            providers = []
        values = {key: self.config.get(key) for key in self._schema_cache}
        return astrbot_web.json_response({
            "version": PLUGIN_VERSION,
            "schema": self._schema_cache,
            "values": values,
            "providers": providers,
        })

    def _normalize_value(self, spec: Dict[str, Any], value):
        """按 schema 归一前端提交的值，不合法返回 (None, False)。"""
        vtype = spec.get("type")
        try:
            if vtype == "bool":
                if isinstance(value, bool):
                    return value, True
                return str(value).strip().lower() in ("1", "true", "yes", "on"), True
            if vtype == "int":
                return int(float(value)), True
            if vtype == "float":
                return float(value), True
            if vtype == "list":
                if isinstance(value, str):
                    value = [line.strip() for line in value.splitlines() if line.strip()]
                return [str(x) for x in (value or [])], True
            if vtype == "object":
                if not isinstance(value, dict):
                    return None, False
                out = {}
                for sub_key, sub_spec in (spec.get("items") or {}).items():
                    if sub_key in value:
                        sub_val, ok = self._normalize_value(sub_spec, value[sub_key])
                        if ok:
                            out[sub_key] = sub_val
                return out, True
            text = str(value)
            options = spec.get("options")
            if options and text not in options:
                return None, False
            return text, True
        except (TypeError, ValueError):
            return None, False

    async def _webui_save_config(self):
        try:
            payload = await astrbot_web.request.json(default={})
        except Exception:
            return astrbot_web.error_response("请求体不是合法 JSON", status_code=400)
        values = payload.get("values") if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            return astrbot_web.error_response('请求体必须是 {"values": {...}}', status_code=400)

        applied = {}
        for key, value in values.items():
            spec = self._schema_cache.get(key)
            if spec is None:
                continue  # 模板外的键一律不收
            norm, ok = self._normalize_value(spec, value)
            if not ok:
                return astrbot_web.error_response(f"配置项 {key} 的值不合法", status_code=400)
            applied[key] = norm

        for key, value in applied.items():
            self.config[key] = value

        try:
            save_async = getattr(self.config, "save_config_async", None)
            if callable(save_async):
                await save_async()
            else:
                self.config.save_config()
        except Exception as e:
            logger.error(f"[RTPM] WebUI 保存配置失败：{e}", exc_info=True)
            return astrbot_web.error_response("配置保存失败，详情见日志", status_code=500)

        self._apply_runtime_config()
        logger.info(f"[RTPM] WebUI 已保存配置：{', '.join(sorted(applied)) or '（无变更）'}")
        return astrbot_web.json_response({"ok": True, "applied": sorted(applied)})

    def _apply_runtime_config(self):
        """配置变更后刷新运行时状态，免重载插件即可生效。"""
        self.waking_regex = self.config.get("waking_regex", []) or []
        self.c_awake = self.config.get("continuous_awakening", {}) or {}
        self.whitelist = self._session_cfg().get("whitelist", []) or []
        self._compile_regex()

    async def _webui_get_status(self):
        interval = float(self.c_awake.get("waking_interval", 30))
        now = time.time()
        sessions = [
            {"umo": umo, "remain": round(max(interval - (now - info.get("last_time", 0)), 0), 1)}
            for umo, info in self.waking_sessions.items()
        ]
        return astrbot_web.json_response({
            "sessions": sessions,
            "continuous_enabled": bool(self.c_awake.get("enable", False)),
            "regex_compiled": [p.pattern for p in self._compiled_regex],
            "history": {sid: len(msgs) for sid, msgs in self.history_cache.items()},
        })

    async def _webui_drop_session(self):
        try:
            payload = await astrbot_web.request.json(default={})
        except Exception:
            return astrbot_web.error_response("请求体不是合法 JSON", status_code=400)
        umo = payload.get("umo") if isinstance(payload, dict) else None
        if not umo:
            return astrbot_web.error_response("缺少 umo", status_code=400)
        removed = self.waking_sessions.pop(str(umo), None)
        return astrbot_web.json_response({"ok": removed is not None})

    async def terminate(self):
        await self._save_history()
        logger.info("[RTPM] Bot唤醒Pro Max 已卸载。")
