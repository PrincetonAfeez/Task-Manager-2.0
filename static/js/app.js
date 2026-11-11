/* Progressive enhancement for the Task Manager UI.
 *
 * Everything here is optional: each feature enhances a control that already works without
 * JavaScript. There is no application state in the client and no privileged logic — the
 * server re-checks every permission this file's interactions imply.
 */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") {
      fn();
    } else {
      document.addEventListener("DOMContentLoaded", fn);
    }
  }

  /* Workspace switcher: a <select> that POSTs (never GETs — switching is a state change
   * and must carry CSRF protection). */
  function initWorkspaceSwitcher() {
    var select = document.getElementById("workspace-select");
    var form = document.getElementById("workspace-switch-form");
    if (!select || !form) return;

    select.addEventListener("change", function () {
      var template = select.getAttribute("data-switch-template") || "";
      form.action = template.replace("__ID__", encodeURIComponent(select.value));
      form.submit();
    });
  }

  /* Board drag-and-drop. Falls back to the per-card move form when unavailable. */
  function initBoard() {
    var board = document.querySelector("[data-board]");
    if (!board || typeof window.htmx === "undefined") return;

    var dragged = null;

    board.addEventListener("dragstart", function (event) {
      var card = event.target.closest("[data-task-id]");
      if (!card) return;
      dragged = card;
      card.classList.add("is-dragging");
      event.dataTransfer.effectAllowed = "move";
      // Firefox refuses to start a drag without payload.
      event.dataTransfer.setData("text/plain", card.getAttribute("data-task-id"));
    });

    board.addEventListener("dragend", function () {
      if (dragged) dragged.classList.remove("is-dragging");
      dragged = null;
      board.querySelectorAll(".is-drop-target").forEach(function (el) {
        el.classList.remove("is-drop-target");
      });
    });

    board.addEventListener("dragover", function (event) {
      var column = event.target.closest("[data-column-id]");
      if (!column || !dragged) return;
      event.preventDefault();
      column.classList.add("is-drop-target");
    });

    board.addEventListener("dragleave", function (event) {
      var column = event.target.closest("[data-column-id]");
      if (column) column.classList.remove("is-drop-target");
    });

    board.addEventListener("drop", function (event) {
      var column = event.target.closest("[data-column-id]");
      if (!column || !dragged) return;
      event.preventDefault();
      column.classList.remove("is-drop-target");

      var target = event.target.closest("[data-task-id]");
      var body = {
        column: column.getAttribute("data-column-id"),
        expected_version: dragged.getAttribute("data-version") || ""
      };
      if (target && target !== dragged) {
        // Dropping onto the upper half of a card means "insert before it".
        var box = target.getBoundingClientRect();
        var before = event.clientY < box.top + box.height / 2;
        body[before ? "after_id" : "before_id"] = target.getAttribute("data-task-id");
      }

      window.htmx.ajax("POST", dragged.getAttribute("data-move-url"), {
        target: dragged,
        swap: "outerHTML",
        values: body
      });
    });
  }

  /* Confirm destructive submissions. The server still re-confirms anything irreversible;
   * this only prevents the accidental click. */
  function initConfirmations() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      var message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) {
        event.preventDefault();
      }
    });
  }

  /* Surface HTMX transport failures instead of leaving the UI silently unchanged. */
  function initErrorReporting() {
    document.body.addEventListener("htmx:responseError", function (event) {
      var flash = document.getElementById("flash");
      if (!flash) return;
      var status = event.detail.xhr.status;
      if (status === 409 || status === 400 || status === 403) return; // server sent a message
      flash.textContent = "That action could not be completed (error " + status + ").";
    });

    document.body.addEventListener("htmx:sendError", function () {
      var flash = document.getElementById("flash");
      if (flash) flash.textContent = "Network error. Check your connection and try again.";
    });
  }

  ready(function () {
    initWorkspaceSwitcher();
    initBoard();
    initConfirmations();
    initErrorReporting();
  });
})();
