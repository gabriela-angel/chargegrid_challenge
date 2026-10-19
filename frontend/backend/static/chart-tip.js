/*
 * ChargeGrid Intelligence — tooltip visual dos gráficos de barras
 * ----------------------------------------------------------------
 * Substitui o balãozinho cinza padrão do navegador (atributo title) por um cartão
 * próprio, igual em todos os gráficos. Autocontido: não depende de app.js.
 *
 * Contrato: qualquer elemento com [data-tip] vira alvo. Atributos lidos:
 *   data-tip-head    título curto (ex.: "18h", "qua, 14/10")
 *   data-tip-main    valor em destaque (ex.: "R$ 2,47/kWh")
 *   data-tip-pill    etiqueta de nível (ex.: "tarifa alta") — opcional
 *   data-tip-tone    ok | warn | critical | neutral — cor da etiqueta e do valor
 *   data-tip-detail  linha de detalhe — opcional
 * O alvo ganha .is-active enquanto o cartão está aberto (o CSS destaca a barra e
 * apaga as vizinhas). Funciona com mouse, teclado (foco) e toque.
 */
(function () {
  "use strict";
  var tip, active = null;

  function build() {
    tip = document.createElement("div");
    tip.className = "cg-tip";
    tip.setAttribute("role", "tooltip");
    tip.setAttribute("aria-hidden", "true");
    tip.innerHTML =
      '<div class="cg-tip-head"></div>' +
      '<div class="cg-tip-main mono"></div>' +
      '<span class="cg-tip-pill"></span>' +
      '<div class="cg-tip-detail"></div>' +
      '<span class="cg-tip-arrow"></span>';
    document.body.appendChild(tip);
  }

  function setText(sel, text) {
    var el = tip.querySelector(sel);
    el.textContent = text || "";
    el.hidden = !text;
  }

  function show(target) {
    if (!tip) build();
    if (active && active !== target) active.classList.remove("is-active");
    active = target;
    target.classList.add("is-active");

    var tone = target.getAttribute("data-tip-tone") || "neutral";
    tip.setAttribute("data-tone", tone);
    setText(".cg-tip-head", target.getAttribute("data-tip-head"));
    setText(".cg-tip-main", target.getAttribute("data-tip-main"));
    setText(".cg-tip-pill", target.getAttribute("data-tip-pill"));
    setText(".cg-tip-detail", target.getAttribute("data-tip-detail"));
    tip.setAttribute("aria-hidden", "false");
    tip.classList.add("is-visible");
    place(target);
  }

  function hide() {
    if (active) active.classList.remove("is-active");
    active = null;
    if (tip) {
      tip.classList.remove("is-visible");
      tip.setAttribute("aria-hidden", "true");
    }
  }

  // Posiciona acima do alvo (ou abaixo, se não couber), sem sair da tela; a setinha
  // continua apontando para o centro do alvo mesmo quando o cartão é deslocado.
  function place(target) {
    var r = target.getBoundingClientRect();
    var w = tip.offsetWidth, h = tip.offsetHeight, gap = 10, margin = 8;
    var cx = r.left + r.width / 2;
    var left = Math.min(Math.max(cx - w / 2, margin), window.innerWidth - w - margin);
    var above = r.top - h - gap >= margin;
    var top = above ? r.top - h - gap : r.bottom + gap;
    tip.style.left = Math.round(left + window.scrollX) + "px";
    tip.style.top = Math.round(top + window.scrollY) + "px";
    tip.setAttribute("data-side", above ? "top" : "bottom");
    tip.style.setProperty("--arrow-x", Math.round(cx - left) + "px");
  }

  function targetOf(e) {
    return e.target.closest ? e.target.closest("[data-tip]") : null;
  }

  document.addEventListener("mouseover", function (e) {
    var t = targetOf(e);
    if (t) show(t); else if (active) hide();
  });
  document.addEventListener("focusin", function (e) {
    var t = targetOf(e);
    if (t) show(t);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
  // Toque: abre no alvo tocado; tocar fora (ou de novo) fecha.
  document.addEventListener("touchstart", function (e) {
    var t = targetOf(e);
    if (t && t !== active) show(t); else hide();
  }, { passive: true });
  window.addEventListener("scroll", hide, { passive: true });
  window.addEventListener("resize", hide);
  // Gráficos trocados pelo HTMX: o alvo antigo some, então fecha o cartão.
  document.addEventListener("htmx:beforeSwap", hide);
})();
