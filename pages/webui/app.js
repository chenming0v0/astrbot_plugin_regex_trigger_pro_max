/* Bot唤醒Pro Max 控制台逻辑（Google Material 风格重写版）。
 * 通过 window.AstrBotPluginPage（AstrBot 插件页面桥）与后端 webui API 通信；
 * 直接用浏览器打开时无桥，显示离线提示。 */
(function () {
  "use strict";

  var bridge = null;
  var schema = null;
  var statusTimer = null;

  var ICONS = {
    add: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>',
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>'
  };

  var LIST_PLACEHOLDER = {
    waking_regex: ".*小辰.*",
    whitelist: "aiocqhttp:GroupMessage:114514"
  };

  var OBJECT_KEYS = {
    continuous_awakening: ["enable", "waking_interval", "reset_when_reply"]
  };

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

  var listData = { waking_regex: [], whitelist: [] };

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

  /* ---------- 列表编辑器：一行一条，按钮增删 ---------- */

  function renderList(key) {
    var box = $("list-" + key);
    box.innerHTML = "";
    var placeholder = LIST_PLACEHOLDER[key] || "";

    listData[key].forEach(function (val, idx) {
      var row = document.createElement("div");
      row.className = "list-row";

      var input = document.createElement("input");
      input.type = "text";
      input.value = val;
      input.placeholder = placeholder;
      input.className = key === "waking_regex" ? "mono" : "";
      input.addEventListener("input", function () { listData[key][idx] = input.value; });

      var del = document.createElement("button");
      del.type = "button";
      del.className = "icon-btn danger";
      del.title = "删除这条";
      del.innerHTML = ICONS.close;
      del.addEventListener("click", function () {
        listData[key].splice(idx, 1);
        renderList(key);
      });

      row.appendChild(input);
      row.appendChild(del);
      box.appendChild(row);
    });

    var add = document.createElement("button");
    add.type = "button";
    add.className = "text-btn";
    add.innerHTML = ICONS.add + "<span>添加一条</span>";
    add.addEventListener("click", function () {
      listData[key].push("");
      renderList(key);
      var rows = box.querySelectorAll(".list-row input");
      if (rows.length) rows[rows.length - 1].focus();
    });
    box.appendChild(add);
  }

  /* ---------- 普通控件读写 ---------- */

  function setFieldValue(key, value) {
    var el = $("field-" + key);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!value;
    else el.value = value == null ? "" : value;
  }

  function getFieldValue(key) {
    var el = $("field-" + key);
    if (!el) return null;
    if (el.type === "checkbox") return el.checked;
    if (el.type === "number") return el.value === "" ? null : Number(el.value);
    return el.value;
  }

  function fillProviderSelect(providers, current) {
    var sel = $("field-analysis_provider_id");
    var wanted = current || "";
    sel.innerHTML = "";

    var optEmpty = document.createElement("option");
    optEmpty.value = "";
    optEmpty.textContent = "留空 = 使用当前默认供应商";
    sel.appendChild(optEmpty);

    providers.forEach(function (p) {
      var opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      sel.appendChild(opt);
    });

    if (wanted && providers.indexOf(wanted) === -1) {
      var optStale = document.createElement("option");
      optStale.value = wanted;
      optStale.textContent = wanted + "（已失效，请重选）";
      sel.appendChild(optStale);
    }
    sel.value = wanted;
  }

  /* ---------- 状态区 ---------- */

  function renderStatus(data) {
    $("st-cont").textContent = data.continuous_enabled ? "已开启" : "已关闭";
    $("st-regex").textContent = (data.regex_compiled || []).join("   ") || "（无）";

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
      label.textContent = s.umo + " · 剩 " + s.remain + "s";

      var kill = document.createElement("button");
      kill.type = "button";
      kill.className = "icon-btn danger";
      kill.title = "退出唤醒";
      kill.innerHTML = ICONS.close;
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

  /* ---------- 保存 ---------- */

  function collectValues() {
    var values = {};
    TOP_KEYS.forEach(function (key) {
      if (listData[key] !== undefined) {
        values[key] = listData[key]
          .map(function (s) { return s.trim(); })
          .filter(function (s) { return s !== ""; });
      } else if (OBJECT_KEYS[key]) {
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
    bridge.apiPost("webui/config", { values: collectValues() }).then(
      function (res) {
        btn.disabled = false;
        var keys = (res && res.applied) || [];
        toast("已保存 " + keys.length + " 项配置，立即生效");
        refreshStatus();
      },
      function (e) {
        btn.disabled = false;
        toast("保存失败：" + (e && e.message ? e.message : e), true);
      }
    );
  }

  /* ---------- 启动 ---------- */

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

  function applyConfig(cfg) {
    schema = cfg.schema || {};
    var values = cfg.values || {};
    $("ver").textContent = cfg.version || "";

    renderHints();
    fillProviderSelect(cfg.providers || [], values.analysis_provider_id);

    listData.waking_regex = (values.waking_regex || []).slice();
    listData.whitelist = (values.whitelist || []).slice();
    renderList("waking_regex");
    renderList("whitelist");

    TOP_KEYS.forEach(function (key) {
      if (listData[key] !== undefined) return;
      if (OBJECT_KEYS[key]) {
        var obj = values[key] || {};
        OBJECT_KEYS[key].forEach(function (sub) {
          setFieldValue(key + "." + sub, obj[sub]);
        });
      } else {
        setFieldValue(key, values[key]);
      }
    });

    $("panels").classList.remove("hidden");
    $("save-btn").disabled = false;
    $("save-btn").onclick = save;

    refreshStatus();
    statusTimer = setInterval(refreshStatus, 5000);
  }

  function boot() {
    waitForBridge(4000).then(function (b) {
      if (!b || typeof b.apiGet !== "function") {
        $("no-bridge").classList.remove("hidden");
        return null;
      }
      bridge = b;
      return bridge.ready ? bridge.ready() : {};
    }).then(function (ctx) {
      if (!ctx || !bridge) return null;
      return bridge.apiGet("webui/config");
    }).then(function (cfg) {
      if (!cfg) return;
      applyConfig(cfg);
    }, function (e) {
      toast("读取配置失败：" + (e && e.message ? e.message : e), true);
    });
  }

  window.addEventListener("beforeunload", function () {
    if (statusTimer) clearInterval(statusTimer);
  });

  boot();
})();
