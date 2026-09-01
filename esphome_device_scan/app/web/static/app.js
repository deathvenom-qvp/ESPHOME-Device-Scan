/* Panel behaviour.
 *
 * No build step and no framework: the page is small enough that plain DOM
 * calls are clearer than a toolchain, and an add-on that ships no bundler is
 * one less thing to break on a Supervisor upgrade.
 *
 * Every fetch URL is relative so it resolves against the <base href> the
 * server injected from X-Ingress-Path.
 */

(function () {
  "use strict";

  var state = { devices: [], templates: [], settings: {}, onlyMissing: false };
  var lastLogId = 0;
  var busy = {};

  var el = {
    summary: document.getElementById("summary"),
    banner: document.getElementById("banner"),
    body: document.getElementById("devices-body"),
    templates: document.getElementById("templates"),
    templatesDir: document.getElementById("templates-dir"),
    logs: document.getElementById("logs"),
    settings: document.getElementById("settings"),
    scanBtn: document.getElementById("scan-btn"),
    filter: document.getElementById("filter-missing"),
    modal: document.getElementById("modal"),
    modalTitle: document.getElementById("modal-title"),
    modalPath: document.getElementById("modal-path"),
    modalBody: document.getElementById("modal-body"),
  };

  // -- helpers ---------------------------------------------------------

  function api(path, options) {
    return fetch(path, options || {}).then(function (response) {
      return response.json()
        .catch(function () { return { error: "HTTP " + response.status }; })
        .then(function (data) {
          if (!response.ok) throw new Error(data.error || "HTTP " + response.status);
          return data;
        });
    });
  }

  function banner(message, kind) {
    if (!message) { el.banner.hidden = true; return; }
    el.banner.textContent = message;
    el.banner.className = "banner" + (kind ? " " + kind : "");
    el.banner.hidden = false;
  }

  function node(tag, className, text) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = text;
    return element;
  }

  function button(label, className, onClick) {
    var b = node("button", "btn btn-small " + (className || ""), label);
    b.addEventListener("click", onClick);
    return b;
  }

  // -- rendering -------------------------------------------------------

  var STATUS_PILL = {
    online: "pill-ok",
    offline: "pill-idle",
    discovered: "pill-warn",
    unknown: "pill-idle",
  };

  // Why a file in the ESPHome directory was taken to be a parent template.
  var DETECTED_BY = {
    "mac-placeholder": "found via ${mac} in its name",
    "mac-suffix-flag": "found via name_add_mac_suffix",
    "directive": "marked # x-template",
  };

  var OUTCOME_LABEL = {
    generated: ["Generated", "pill-ok"],
    regenerated: ["Regenerated", "pill-ok"],
    would_generate: ["Would generate", "pill-warn"],
    skipped_has_config: ["Yes", "pill-ok"],
    skipped_auto_generate_off: ["Pending", "pill-warn"],
    no_template_match: ["No template", "pill-warn"],
    error: ["Error", "pill-err"],
  };

  function renderDevices() {
    var rows = state.devices.filter(function (d) {
      return !state.onlyMissing || !d.has_yaml;
    });

    el.body.textContent = "";

    if (!rows.length) {
      var tr = node("tr");
      var td = node("td", "empty", state.devices.length
        ? "Every discovered device already has a config."
        : "No ESPHome devices found. Add them to Home Assistant's ESPHome integration first.");
      td.colSpan = 7;
      tr.appendChild(td);
      el.body.appendChild(tr);
      return;
    }

    rows.forEach(function (device) {
      el.body.appendChild(renderRow(device));
    });
  }

  function renderRow(device) {
    var tr = node("tr");

    var nameCell = node("td");
    nameCell.appendChild(node("div", "device-name", device.display_name));
    if (device.model || device.sw_version) {
      nameCell.appendChild(node("div", "device-sub",
        [device.model, device.sw_version].filter(Boolean).join(" · ")));
    }
    tr.appendChild(nameCell);

    tr.appendChild(node("td", "mono", device.node_name));
    tr.appendChild(node("td", "mono dim", device.mac || "—"));

    var statusCell = node("td");
    statusCell.appendChild(node("span", "pill " + (STATUS_PILL[device.status] || "pill-idle"),
      device.status));
    tr.appendChild(statusCell);

    var configCell = node("td");
    var label = OUTCOME_LABEL[device.outcome] || (device.has_yaml ? ["Yes", "pill-ok"] : ["No", "pill-warn"]);
    var pill = node("span", "pill " + label[1], label[0]);
    if (device.message) pill.title = device.message;
    configCell.appendChild(pill);
    tr.appendChild(configCell);

    var templateCell = node("td", "mono dim", device.template || "—");
    if (device.match_rule) templateCell.title = "matched by " + device.match_rule;
    tr.appendChild(templateCell);

    tr.appendChild(renderActions(device));
    return tr;
  }

  function renderActions(device) {
    var cell = node("td", "actions");
    var name = device.node_name;

    if (device.has_yaml) {
      cell.appendChild(button("View", "", function () { viewYaml(name); }));
      cell.appendChild(button("Regenerate", "", function () { regenerate(name); }));
    } else if (device.template) {
      cell.appendChild(button("Preview", "", function () { preview(name); }));
      cell.appendChild(button("Generate", "btn-primary", function () { generate(name); }));
    } else {
      cell.appendChild(node("span", "dim", "no template"));
    }

    if (busy[name]) {
      Array.prototype.forEach.call(cell.querySelectorAll("button"), function (b) {
        b.disabled = true;
      });
    }
    return cell;
  }

  function renderTemplates() {
    el.templates.textContent = "";
    if (!state.templates.length) {
      el.templates.appendChild(node("p", "empty",
        "No parent templates found. A parent is a base config in your ESPHome "
        + "directory with MAC-suffix logic \u2014 name: <family>-${mac}, or "
        + "name_add_mac_suffix: true \u2014 or any file marked '# x-template: true'."));
      return;
    }

    state.templates.forEach(function (template) {
      var row = node("div", "template-row");
      row.appendChild(node("span", "template-name", template.name));

      var rules = [];
      if (template.regexes.length) rules.push("regex " + template.regexes.join(", "));
      if (template.prefixes.length) {
        rules.push("claims " + template.prefixes.join("-*, ") + "-*");
      }
      if (template.models.length) rules.push("model " + template.models.join(", "));
      if (template.mac_policy) rules.push("mac: " + template.mac_policy);
      if (template.detected_by) rules.push(DETECTED_BY[template.detected_by] || template.detected_by);

      row.appendChild(node("span", "template-rules", rules.join(" · ")));
      el.templates.appendChild(row);

      (template.warnings || []).forEach(function (warning) {
        var warn = node("div", "template-row");
        warn.appendChild(node("span", "template-rules", "⚠ " + warning));
        el.templates.appendChild(warn);
      });
    });
  }

  function renderSettings() {
    var labels = {
      esphome_config_dir: "ESPHome config dir",
      scan_interval_minutes: "Scan interval (min)",
      auto_generate: "Auto-generate",
      dry_run: "Dry run",
      mac_policy: "MAC policy",
      name_add_mac_suffix_action: "MAC-suffix flag",
    };
    el.settings.textContent = "";
    Object.keys(labels).forEach(function (key) {
      if (state.settings[key] === undefined) return;
      el.settings.appendChild(node("dt", null, labels[key]));
      el.settings.appendChild(node("dd", null, String(state.settings[key])));
    });
  }

  function renderLogs(entries) {
    if (!entries.length) return;
    if (el.logs.querySelector(".empty")) el.logs.textContent = "";

    var atBottom = el.logs.scrollHeight - el.logs.scrollTop - el.logs.clientHeight < 40;

    entries.forEach(function (entry) {
      lastLogId = Math.max(lastLogId, entry.id);
      var line = node("div", "log-line log-" + entry.level);
      line.appendChild(node("span", "log-time", entry.ts.replace("T", " ").replace("+00:00", "")));
      line.appendChild(node("span", "log-msg", entry.message));
      el.logs.appendChild(line);
    });

    // Only auto-scroll when the user was already at the bottom, so reading
    // back through the log is not yanked away by new lines arriving.
    if (atBottom) el.logs.scrollTop = el.logs.scrollHeight;
  }

  // -- actions ---------------------------------------------------------

  function applyScan(scan) {
    state.devices = scan.devices || [];
    el.summary.textContent = scan.summary || "";
    if (scan.errors && scan.errors.length) {
      banner(scan.errors.join(" · "), "");
    } else {
      banner(null);
    }
    renderDevices();
  }

  function refresh() {
    return api("api/state").then(function (data) {
      state.templates = data.templates || [];
      state.settings = data.settings || {};
      applyScan(data.scan || {});
      renderTemplates();
      renderSettings();
      el.templatesDir.textContent = state.settings.esphome_config_dir
        ? "read from " + state.settings.esphome_config_dir
        : "";
    }).catch(function (err) {
      banner("Could not load state: " + err.message);
    });
  }

  function scan() {
    el.scanBtn.disabled = true;
    el.scanBtn.textContent = "Scanning…";
    api("api/scan", { method: "POST" })
      .then(function (data) { applyScan(data); return refresh(); })
      .catch(function (err) { banner("Scan failed: " + err.message); })
      .then(function () {
        el.scanBtn.disabled = false;
        el.scanBtn.textContent = "Scan now";
        pollLogs();
      });
  }

  function withBusy(name, promise) {
    busy[name] = true;
    renderDevices();
    return promise.then(function (result) {
      delete busy[name];
      return result;
    }, function (err) {
      delete busy[name];
      renderDevices();
      throw err;
    });
  }

  function generate(name) {
    withBusy(name, api("api/generate/" + encodeURIComponent(name), { method: "POST" }))
      .then(function () { return refresh(); })
      .catch(function (err) { banner("Generate failed for " + name + ": " + err.message); })
      .then(pollLogs);
  }

  function regenerate(name) {
    if (!window.confirm(
      "Regenerate " + name + ".yaml from its template?\n\n" +
      "The current file is backed up alongside it as .bak-<timestamp>, " +
      "but any edits you made will not be carried into the new file."
    )) return;

    withBusy(name, api("api/regenerate/" + encodeURIComponent(name), { method: "POST" }))
      .then(function () { return refresh(); })
      .catch(function (err) { banner("Regenerate failed for " + name + ": " + err.message); })
      .then(pollLogs);
  }

  function viewYaml(name) {
    api("api/yaml/" + encodeURIComponent(name))
      .then(function (data) { openModal(name + ".yaml", data.path, data.content); })
      .catch(function (err) { banner("Could not open YAML: " + err.message); });
  }

  function preview(name) {
    api("api/preview/" + encodeURIComponent(name))
      .then(function (data) {
        var note = "Preview from " + data.template + " — nothing has been written.";
        if (data.warnings && data.warnings.length) note += "\n⚠ " + data.warnings.join("\n⚠ ");
        openModal("Preview: " + name + ".yaml", note, data.content);
      })
      .catch(function (err) { banner("Could not preview: " + err.message); });
  }

  function openModal(title, path, content) {
    el.modalTitle.textContent = title;
    el.modalPath.textContent = path || "";
    el.modalBody.textContent = content;
    el.modal.hidden = false;
  }

  function closeModal() { el.modal.hidden = true; }

  function pollLogs() {
    return api("api/logs?since=" + lastLogId)
      .then(function (data) { renderLogs(data.entries || []); })
      .catch(function () { /* the banner already covers real outages */ });
  }

  // -- wiring ----------------------------------------------------------

  el.scanBtn.addEventListener("click", scan);
  el.filter.addEventListener("change", function () {
    state.onlyMissing = el.filter.checked;
    renderDevices();
  });
  document.getElementById("clear-log").addEventListener("click", function () {
    el.logs.textContent = "";
    el.logs.appendChild(node("p", "empty", "Cleared. New activity will appear here."));
  });
  document.getElementById("modal-close").addEventListener("click", closeModal);
  el.modal.addEventListener("click", function (event) {
    if (event.target === el.modal) closeModal();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !el.modal.hidden) closeModal();
  });

  refresh().then(pollLogs);
  setInterval(pollLogs, 5000);
  setInterval(refresh, 30000);
})();
