# astrbot_plugin_regex_trigger_pro_max

把 `astrbot_plugin_wake_enhance`（正则唤醒 / 持续唤醒）和 `should_I_respond`（小模型判定该不该回）融合成一个插件。

原插件源码保留在 `temp/` 目录下，只作参考，AstrBot 不会加载它们。

## 为什么要融合

两个插件分开装的时候，唤醒流程是割裂的：

- wake_enhance 只管把 `is_at_or_wake_command` 置成 True，不区分是正则蹭到的还是真被 @ 了
- should_I_respond 不管唤醒来源，一律丢给小模型判一遍，被直接 @ 也可能判成不回，白烧 token 还显得不理人

融合之后引入了**唤醒来源**的概念：

| 来源 | 含义 | 判定提示 |
| --- | --- | --- |
| `native` | @ 你、唤醒前缀、指令 | 告诉小模型这条是直接点名，通常该回 |
| `regex` | 命中唤醒词正则 | 告诉小模型这是软唤醒，谨慎判断 |
| `continuous` | 处于持续唤醒窗口内 | 告诉小模型可能只是群友闲聊，谨慎判断 |

三种来源都会走一遍小模型，只是给的上下文不同。

来源会一并塞进判定提示词的 `{awakening_context}`，小模型知道这条是「软唤醒」还是「被点名」，判得更准。

## 执行流程

```
消息进来
  ↓
wake_listener  正则匹配 / 持续窗口判定 → 标记唤醒来源
  ↓
AstrBot 决定要不要调 LLM
  ↓
interest_analyzer  按来源决定是否调小模型
  ↓  should_reply=false → stop_event，不回
  ↓  should_reply=true  → 注入 interest / feeling
主模型生成回复
  ↓
save_llm_reply_to_history  回复入自管历史
```

## 配置要点

- `waking_regex`：唤醒正则，改完保存后重载插件生效，坏正则会跳过而不是整条流程炸掉
- `whitelist`：全插件唯一白名单，填 UMO，**留空 = 不限制**，正则唤醒 / 持续唤醒 / 小模型判定都受它管
- `analysis_provider_id`：判定用的小模型，留空则判定环节整体不生效
- 被点名不跳过判定：任何唤醒方式都会走一遍小模型
- `inject_emotion`：关掉就只做拦不拦，不改 prompt

## 与原插件的差异

- 唤醒正则从 `re.match` 改成 `re.search`，`.*小辰.*` 这种写法不写星号也能命中
- 正则预编译，写错的表达式只跳过该条并打日志
- 群号统一转字符串比对，避免配置里填成数字导致白名单失效
- 历史记录落盘路径改为 `data/rtpm_interest_history.json`，不和原插件抢文件
- 历史记录多存了 `wake_source` 字段，翻文件就能看出每条是怎么被唤醒的
- `_save_history` 收进 `finally`，分析中途抛异常也不丢记录
- 不注册任何斜杠指令，全部行为由配置面板控制

## 注意

装这个之前请先把 `astrbot_plugin_wake_enhance` 和 `should_I_respond` 停用，否则唤醒判定会跑两遍。
