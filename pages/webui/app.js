/* Bot唤醒Pro Max 控制台逻辑。
 * 通过 window.AstrBotPluginPage（AstrBot 插件页面桥）与后端 webui API 通信；
 * 直接用浏览器打开时无桥，显示离线提示。 */
(function () {
  "use strict";

  var bridge = null;
  var schema = null;
  var statusTimer = null;

  var OBJECT_KEYS = {
    continuous_awakening: ["enable", "waking_interval", "reset_when_reply"]
  };

  // 需要收集提交的顶层配置键，与 _conf_schema.json 一一对应
  var TOP_KEYS = [
    "waking_regex",
    "continuous_awakening",
    "analysis_provider_id",
    "analysis_fail_policy",
    "random_reply_chance",
    "inject_emotion",
    "whitelist",
    "history_max_length",
    "record_emotion_in_history",
    "analysis_system_prompt"
  ];

  function $(id) { return document.getElementById(id); }

  function toast(msg, isErr) {
    var el = $("toast");
    el.textContent = msg;
    el.classList.toggle("err", !!isErr);
    el.classList.remove("hidden");
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.classList.add("hidden"); }, 2600);
  }

  function specOf(path) {
    if (!schema) return null;
    var parts = path.split(".");
    var node = schema[parts[0]];
    if (parts.length > 1 && node && node.items) node = node.items[parts[1]];
    return node || null;
  }

  function renderHints() {
    var nodes = document.querySelectorAll("[data-hint]");
    for (var i = 0; i < nodes.length; i++) {
      var spec = specOf(nodes[i].getAttribute("data-hint"));
      if (!spec) continue;
      var parts = [];
      if (spec.description) parts.push(spec.description);
      if (spec.hint) parts.push(spec.hint);
      nodes[i].textContent = parts.join(" — ");
    }
  }

  function setFieldValue(key, value) {
    var el = $("field-" + key);
    if (!el) return;
    if (el.type === "checkbox") {
      el.checked = !!value;
    } else if (el.tagName === "TEXTAREA" && Array.isArray(value)) {
      el.value = value.join("\n");
    } else {
      el.value = value == null ? "" : value;
    }
  }

  function getFieldValue(key) {
    var el = $("field-" + key);
    if (!el) return null;
    if (el.type === "checkbox") return el.checked;
    if (el.tagName === "TEXTAREA" && Array.isArray((schema[key] || {}).default || [])) {
      return el.value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
    }
    if (el.type === "number") return el.value === "" ? null : Number(el.value);
    return el.value;
  }

  function fillProviderSelect(providers, current) {
    var sel = $("field-analysis_provider_id");
    var wanted = current || "";
    var exists = providers.some(function (p) { return p === wanted; });
    sel.innerHTML = "";
    var optEmpty = document.createElement("option");
    optEmpty.value = "";
    optEmpty.textContent = "（留空 = 当前默认供应商）";
    sel.appendChild(optEmpty);
    providers.forEach(function (p) {
      var opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      sel.appendChild(opt);
    });
    if (wanted && !exists) {
      var optStale = document.createElement("option");
      optStale.value = wanted;
      optStale.textContent = wanted + "（已失效，请重选）";
      sel.appendChild(optStale);
    }
    sel.value = wanted;
  }

  function renderStatus(data) {
    $("st-cont").textContent = data.continuous_enabled ? "ON" : "OFF";
    $("st-regex").textContent = (data.regex_compiled || []).join("  ") || "（无）";

    var box = $("st-sessions");
    box.innerHTML = "";
    var sessions = data.sessions || [];
    if (!sessions.length) {
      box.textContent = "（无）";
      return;
    }
    sessions.sort(function (a, b) { return a.remain - b.remain; }).forEach(function (s) {
      var chip = document.createElement("span");
      chip.className = "session-chip";
      var label = document.createElement("span");
      label.textContent = s.umo + " · " + s.remain + "s";
      var kill = document.createElement("button");
      kill.className = "kill";
      kill.type = "button";
      kill.title = "退出唤醒";
      kill.textContent = "✕";
      kill.onclick = function () {
        bridge.apiPost("webui/session", { umo: s.umo }).then(function () {
          toast("已退出：" + s.umo);
          refreshStatus();
        }, function (e) { toast("操作失败：" + (e && e.message ? e.message : e), true); });
      };
      chip.appendChild(label);
      chip.appendChild(kill);
      box.appendChild(chip);
    });
  }

  function refreshStatus() {
    if (!bridge) return;
    bridge.apiGet("webui/status").then(renderStatus, function () { /* 静默，下次轮询再试 */ });
  }

  function collectValues() {
    var values = {};
    TOP_KEYS.forEach(function (key) {
      if (OBJECT_KEYS[key]) {
        var obj = {};
        OBJECT_KEYS[key].forEach(function (sub) {
          obj[sub] = getFieldValue(key + "." + sub);
        });
        values[key] = obj;
      } else {
        values[key] = getFieldValue(key);
      }
    });
    return values;
  }

  function save() {
    var btn = $("save-btn");
    btn.disabled = true;
    btn.textContent = "保存中…";
    bridge.apiPost("webui/config", { values: collectValues() }).then(
      function (res) {
        btn.disabled = false;
        btn.textContent = "保存配置";
        var keys = (res && res.applied) || [];
        toast("已保存 " + keys.length + " 项配置，立即生效");
        refreshStatus();
      },
      function (e) {
        btn.disabled = false;
        btn.textContent = "保存配置";
        toast("保存失败：" + (e && e.message ? e.message : e), true);
      }
    );
  }

  function waitForBridge(timeoutMs) {
    // 桥由宿主注入，可能晚于本脚本执行（index.html 已显式引入 SDK，这里再兜底轮询）
    return new Promise(function (resolve) {
      if (window.AstrBotPluginPage) { resolve(window.AstrBotPluginPage); return; }
      var waited = 0;
      var timer = setInterval(function () {
        if (window.AstrBotPluginPage) { clearInterval(timer); resolve(window.AstrBotPluginPage); return; }
        waited += 100;
        if (waited >= timeoutMs) { clearInterval(timer); resolve(null); }
      }, 100);
    });
  }

  function boot() {
    waitForBridge(4000).then(function (b) {
      if (!b || typeof b.apiGet !== "function") {
        $("no-bridge").classList.remove("hidden");
        return null;
      }
      bridge = b;
      var ready = bridge.ready ? bridge.ready() : Promise.resolve();
      return ready;
    }).then(function (okToLoad) {
      if (!okToLoad || !bridge) return;
      return bridge.apiGet("webui/config");
    }).then(function (cfg) {
      if (!cfg) return;
      schema = cfg.schema || {};
      $("ver").textContent = cfg.version || "";

      renderHints();
      fillProviderSelect(cfg.providers || [], (cfg.values || {}).analysis_provider_id);

      TOP_KEYS.forEach(function (key) {
        if (OBJECT_KEYS[key]) {
          var obj = (cfg.values || {})[key] || {};
          OBJECT_KEYS[key].forEach(function (sub) {
            setFieldValue(key + "." + sub, obj[sub]);
          });
        } else {
          setFieldValue(key, (cfg.values || {})[key]);
        }
      });

      $("panels").classList.remove("hidden");
      $("save-btn").disabled = false;
      $("save-btn").onclick = save;

      refreshStatus();
      statusTimer = setInterval(refreshStatus, 5000);
    }, function (e) {
      toast("读取配置失败：" + (e && e.message ? e.message : e), true);
    });
  }

  window.addEventListener("beforeunload", function () {
    if (statusTimer) clearInterval(statusTimer);
  });

  boot();
})();
