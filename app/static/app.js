"use strict";
/*
 * Минимальный JavaScript (2.1.1, 3.3): подтверждение опасных действий, проверка
 * формата ключа, блокировка кнопки отправки, копирование, имя файла, счётчик символов.
 */
(function () {
  function onReady(fn) {
    if (document.readyState !== "loading") {
      fn();
    } else {
      document.addEventListener("DOMContentLoaded", fn);
    }
  }

  function stop(event) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }

  onReady(function () {
    // Подтверждение опасных действий: удаление, перевыпуск ключа, отмена запроса.
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.getAttribute("data-confirm"))) {
          stop(event);
        }
      });
    });

    // Клиентская проверка формата ключа после удаления пробелов.
    document.querySelectorAll("form[data-key-form]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        var input = form.querySelector("input[name=key]");
        var error = form.querySelector("[data-key-error]");
        var value = input.value.replace(/\s+/g, "");
        if (!/^[0-9a-fA-F]{64}$/.test(value)) {
          stop(event);
          input.setAttribute("aria-invalid", "true");
          if (error) {
            error.hidden = false;
          }
        }
      });
    });

    // Блокировка кнопки отправки с текстом загрузки.
    document.querySelectorAll("form").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (event.defaultPrevented) {
          return;
        }
        var button = form.querySelector("button[type=submit][data-loading]");
        if (button) {
          button.disabled = true;
          button.setAttribute("aria-busy", "true");
          button.textContent = button.getAttribute("data-loading");
        }
      });
    });

    // Копирование в буфер обмена.
    document.querySelectorAll("[data-copy-target]").forEach(function (button) {
      button.addEventListener("click", function () {
        var target = document.querySelector(button.getAttribute("data-copy-target"));
        if (!target || !navigator.clipboard) {
          return;
        }
        var text = target.textContent;
        if (button.hasAttribute("data-copy-strip")) {
          text = text.replace(/\s+/g, "");
        }
        navigator.clipboard.writeText(text).then(function () {
          var original = button.textContent;
          button.textContent = "Скопировано";
          window.setTimeout(function () {
            button.textContent = original;
          }, 2000);
        });
      });
    });

    // Имя выбранного файла.
    document.querySelectorAll("input[type=file][data-file-name]").forEach(function (input) {
      var output = document.querySelector(input.getAttribute("data-file-name"));
      input.addEventListener("change", function () {
        if (output) {
          output.textContent = input.files.length ? "Выбран файл: " + input.files[0].name : "";
        }
      });
    });

    // Счётчик символов текстовой записи «N / 10 000».
    document.querySelectorAll("textarea[data-counter]").forEach(function (area) {
      var output = document.querySelector(area.getAttribute("data-counter"));
      var max = Number(area.getAttribute("data-max"));
      function update() {
        if (output) {
          var length = area.value.replace(/\r\n/g, "\n").length;
          output.textContent = length.toLocaleString("ru-RU") + " / " + max.toLocaleString("ru-RU");
        }
      }
      area.addEventListener("input", update);
      update();
    });
  });
})();
