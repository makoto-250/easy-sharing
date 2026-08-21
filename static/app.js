"use strict";

var KEY_ALPHABET =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
var KEY_LENGTH = 16;

/* --- トースト -------------------------------------------------------- */

var toastTimer = null;

function showToast(message) {
  var toast = document.getElementById("toast");
  if (!toast) {
    return;
  }
  toast.textContent = message;
  toast.hidden = false;
  toast.classList.add("is-visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(function () {
    toast.classList.remove("is-visible");
    toast.hidden = true;
  }, 2500);
}

/* --- 共有キーの自動生成（Web Crypto API / 仕様 4.3）------------------ */

function generateShareKey() {
  var out = "";
  var limit = 256 - (256 % KEY_ALPHABET.length); // 剰余バイアスを避ける
  var buffer = new Uint8Array(KEY_LENGTH * 2);
  while (out.length < KEY_LENGTH) {
    window.crypto.getRandomValues(buffer);
    for (var i = 0; i < buffer.length && out.length < KEY_LENGTH; i++) {
      if (buffer[i] < limit) {
        out += KEY_ALPHABET.charAt(buffer[i] % KEY_ALPHABET.length);
      }
    }
  }
  return out;
}

/* --- コピー（Clipboard API / 仕様 5.3）------------------------------- */

function selectElementText(element) {
  try {
    if (element.tagName === "INPUT" || element.tagName === "TEXTAREA") {
      element.focus();
      element.select();
      return;
    }
    var range = document.createRange();
    range.selectNodeContents(element);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  } catch (err) {
    /* 選択できない環境では何もしない */
  }
}

function textOf(element) {
  return element.tagName === "INPUT" || element.tagName === "TEXTAREA"
    ? element.value
    : element.textContent;
}

function copyFrom(element) {
  var text = textOf(element);
  if (!navigator.clipboard || !window.isSecureContext) {
    selectElementText(element);
    showToast("コピーできませんでした。選択された文字をコピーしてください。");
    return;
  }
  navigator.clipboard.writeText(text).then(
    function () {
      showToast("コピーしました");
    },
    function () {
      // 失敗時は選択状態にして手動コピーを案内する（仕様 5.3）。
      selectElementText(element);
      showToast("コピーできませんでした。選択された文字をコピーしてください。");
    }
  );
}

/* --- 画面ごとの初期化 ------------------------------------------------ */

function setupCopyButtons() {
  var buttons = document.querySelectorAll("[data-copy-target]");
  Array.prototype.forEach.call(buttons, function (button) {
    button.addEventListener("click", function () {
      var target = document.querySelector(button.getAttribute("data-copy-target"));
      if (target) {
        copyFrom(target);
      }
    });
  });
}

function setupShareForm() {
  var form = document.getElementById("share-form");
  if (!form) {
    return;
  }

  var paneText = document.getElementById("pane-text");
  var paneFile = document.getElementById("pane-file");
  var textBody = document.getElementById("text_body");
  var fileInput = document.getElementById("file");
  var fileInfo = document.getElementById("file-info");
  var counter = document.getElementById("text-count");
  var submit = document.getElementById("submit-share");

  function applyMode() {
    var checked = form.querySelector("input[name='data_type']:checked");
    var mode = checked ? checked.value : "text";
    paneText.hidden = mode !== "text";
    paneFile.hidden = mode === "text";
    // 非表示側の入力値は送信対象に含めない（仕様 4.1）。
    textBody.disabled = mode !== "text";
    fileInput.disabled = mode === "text";
  }

  function updateCounter() {
    if (counter) {
      counter.textContent = String(textBody.value.length);
    }
  }

  Array.prototype.forEach.call(
    form.querySelectorAll("input[name='data_type']"),
    function (radio) {
      radio.addEventListener("change", applyMode);
    }
  );

  textBody.addEventListener("input", updateCounter);

  fileInput.addEventListener("change", function () {
    if (fileInput.files && fileInput.files.length === 1) {
      var file = fileInput.files[0];
      var mib = file.size / (1024 * 1024);
      fileInfo.textContent = file.name + " / " + mib.toFixed(2) + " MB";
      fileInfo.hidden = false;
    } else {
      fileInfo.hidden = true;
    }
  });

  var generate = document.getElementById("generate-key");
  if (generate) {
    generate.addEventListener("click", function () {
      // 押すたびに再生成する（仕様 4.2）。
      document.getElementById("share_key").value = generateShareKey();
    });
  }

  form.addEventListener("submit", function () {
    // 二重送信を防止する（仕様 4.2）。サーバー側の PRG と合わせた二重の対策。
    submit.disabled = true;
    submit.textContent = "送信中…";
    window.setTimeout(function () {
      submit.disabled = false;
      submit.textContent = "共有する";
    }, 20000);
  });

  applyMode();
  updateCounter();
}

function setupConfirmForms() {
  var forms = document.querySelectorAll("form[data-confirm]");
  Array.prototype.forEach.call(forms, function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  });
}

document.addEventListener("DOMContentLoaded", function () {
  setupCopyButtons();
  setupShareForm();
  setupConfirmForms();
});
