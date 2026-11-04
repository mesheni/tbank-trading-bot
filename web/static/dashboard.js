/* Read-only dashboard refresh (T4).
 *
 * Polls /api/state at the interval from body[data-refresh-ms] and updates
 * text nodes only. DOM is rebuilt with createElement/textContent - never with
 * markup parsing - and the CSP allows no inline script.
 */
(function () {
  "use strict";

  function asText(value) {
    return value === null || value === undefined ? "--" : String(value);
  }

  function asCell(value) {
    return value === null || value === undefined ? "" : String(value);
  }

  function rub(value) {
    if (value === null || value === undefined) return "--";
    var number = Number(value);
    if (!isFinite(number)) return "--";
    return number.toLocaleString("ru-RU", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    }) + " \u20bd";
  }

  function signedRub(value) {
    if (value === null || value === undefined) return "--";
    var number = Number(value);
    if (!isFinite(number)) return "--";
    var sign = number >= 0 ? "+" : "";
    return sign + number.toLocaleString("ru-RU", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    }) + " \u20bd";
  }

  function age(value) {
    if (value === null || value === undefined) return "нет отметки";
    var number = Number(value);
    if (!isFinite(number)) return "нет отметки";
    return number.toFixed(1) + " мин назад";
  }

  function pnl(value) {
    if (!value || typeof value !== "object") return "—";
    var absRub = Number(value.abs_rub);
    if (!isFinite(absRub)) return "—";
    var sign = absRub >= 0 ? "+" : "";
    var money = sign + absRub.toLocaleString("ru-RU", {
      maximumFractionDigits: 0
    }) + " \u20bd";
    if (value.pct === null || value.pct === undefined) return money;
    var pct = Number(value.pct);
    if (!isFinite(pct)) return money;
    return money + " (" + (pct >= 0 ? "+" : "") + (pct * 100).toFixed(1) + "%)";
  }

  function killSwitch(state) {
    if (!state) return "нет";
    if (state.corrupt) return "файл повреждён";
    return "АКТИВЕН — " + (state.reason || state.kind || "активен");
  }

  function setText(id, value) {
    var node = document.getElementById(id);
    if (node) node.textContent = value;
  }

  function fillRows(id, rows, fields) {
    var body = document.getElementById(id);
    if (!body) return;
    body.textContent = "";
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      fields.forEach(function (field) {
        var td = document.createElement("td");
        var format = field.format || asCell;
        td.textContent = format(row[field.key]);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }

  function daemonOf(snapshot) {
    return snapshot.daemon && typeof snapshot.daemon === "object" ? snapshot.daemon : {};
  }

  function render(snapshot) {
    var daemon = daemonOf(snapshot);
    var positions = Array.isArray(snapshot.positions) ? snapshot.positions : [];
    var journal = Array.isArray(snapshot.journal) ? snapshot.journal : [];
    var logLines = Array.isArray(snapshot.log) ? snapshot.log : [];
    var equityPoint = snapshot.equity && typeof snapshot.equity === "object" ? snapshot.equity : {};
    var equity = daemon.equity_rub;
    var cash = daemon.cash_rub;
    if (equity === null || equity === undefined) equity = equityPoint.total_rub;
    if (cash === null || cash === undefined) cash = equityPoint.cash_rub;
    var positionsCount = daemon.positions_count;
    if (positionsCount === null || positionsCount === undefined) {
      positionsCount = positions.length;
    }

    var badge = document.getElementById("badge");
    var badgeText = asText(snapshot.badge);
    setText("badge", badgeText);
    if (badge) {
      badge.className = "badge badge-" + badgeText.toLowerCase().replace(/[^a-z]/g, "");
    }
    setText("state", asText(daemon.state));
    setText("pid", asText(daemon.pid));
    setText("iteration", asText(daemon.iteration));
    setText("updated", asText(daemon.updated_at));
    setText("heartbeat", age(daemon.heartbeat_age_min));
    setText("last-trade", age(snapshot.journal_last_age_min));
    setText("equity", rub(equity));
    setText("cash", rub(cash));
    setText("pnl", pnl(snapshot.session_pnl));
    setText("positions-count", asText(positionsCount));
    setText("pending", asText(snapshot.pending_orders));
    setText("kill-switch", killSwitch(snapshot.kill_switch));
    setText("status", "обновлено: " + asText(snapshot.generated_at));

    fillRows("positions-body", positions, [
      { key: "ticker" },
      { key: "lots" },
      { key: "price", format: rub },
      { key: "pnl_rub", format: signedRub }
    ]);
    var empty = document.getElementById("positions-empty");
    if (empty) empty.style.display = positions.length ? "none" : "block";

    fillRows("journal-body", journal, [
      { key: "time" },
      { key: "ticker" },
      { key: "action" },
      { key: "lots" },
      { key: "price" }
    ]);

    setText("log", logLines.length ? logLines.join("\n") : "лог пуст");
  }

  function refresh() {
    fetch("/api/state", {
      credentials: "same-origin",
      headers: { "Accept": "application/json" }
    })
      .then(function (response) {
        if (response.status === 401) {
          window.location.replace("/login");
          return null;
        }
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (snapshot) {
        if (snapshot) render(snapshot);
      })
      .catch(function () {
        setText("status", "нет связи с сервером");
      });
  }

  var refreshMs = parseInt(document.body.getAttribute("data-refresh-ms"), 10);
  if (!refreshMs || refreshMs < 1000) refreshMs = 5000;
  window.setInterval(refresh, refreshMs);
})();
