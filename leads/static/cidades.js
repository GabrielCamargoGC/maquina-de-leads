/*
  Campo de cidades com etiquetas e autocompletar.
  -------------------------------------------------------------------------
  O campo antigo era um texto solto onde as cidades iam separadas por
  virgula. Funciona para quem escreveu o sistema e falha para todo o resto:
  a regra ficava numa legenda cinza que ninguem le, entao "Assis e Marilia"
  virava uma busca por uma cidade chamada "assis e marilia" -- e o erro so
  aparecia DEPOIS da busca inteira.

  Aqui a cidade vira etiqueta no momento em que e escolhida, e so pode ser
  escolhida da lista que existe na base. Erro de digitacao deixa de existir.

  Melhoria progressiva: o HTML entregue pelo servidor e o campo de texto de
  sempre, com name="cidades". Este script o esconde e desenha a versao com
  etiquetas por cima, gravando nele a mesma string com virgulas. Se o script
  nao carregar, o formulario continua o de antes -- e o servidor nunca soube
  da diferenca.
*/
(function () {
  "use strict";

  var MAX_LISTA = 8;          // sugestoes visiveis por vez
  var cache = null;           // promessa da lista, compartilhada na pagina

  function normalizar(t) {
    return (t || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toUpperCase()
      .trim();
  }

  function bonito(nome) {
    // A Receita grava tudo em caixa alta. "MARILIA" gritando dentro de uma
    // etiqueta fica agressivo; as preposicoes ficam minusculas para ler como
    // nome proprio ("Sao Joao da Boa Vista").
    var miudas = ["DA", "DE", "DO", "DAS", "DOS", "E", "D"];
    return String(nome || "").split(/\s+/).map(function (p, i) {
      var b = p.charAt(0) + p.slice(1).toLowerCase();
      if (i > 0 && miudas.indexOf(p) >= 0) return p.toLowerCase();
      return b;
    }).join(" ");
  }

  function milhar(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  }

  function carregar() {
    if (cache) return cache;
    cache = fetch("/api/cidades", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { cidades: [] }; })
      .then(function (d) {
        return (d.cidades || []).map(function (c) {
          return { nome: c[0], uf: c[1], qtd: c[2], norm: normalizar(c[0]) };
        });
      })
      .catch(function () { return []; });   // sem lista, o campo aceita texto livre
    return cache;
  }

  function montar(original) {
    var lista = [];                       // cidades escolhidas: {nome, uf}
    var todas = [];
    var marcado = -1;                     // item destacado no dropdown

    var caixa = document.createElement("div");
    caixa.className = "cidades-caixa";

    var entrada = document.createElement("input");
    entrada.type = "text";
    entrada.className = "cidades-entrada";
    entrada.autocomplete = "off";
    entrada.setAttribute("role", "combobox");
    entrada.setAttribute("aria-expanded", "false");
    entrada.setAttribute("aria-autocomplete", "list");
    entrada.placeholder = "Digite e escolha na lista";

    var menu = document.createElement("ul");
    menu.className = "cidades-menu";
    menu.setAttribute("role", "listbox");
    menu.hidden = true;

    var envolve = document.createElement("div");
    envolve.className = "cidades-campo";
    envolve.appendChild(caixa);
    envolve.appendChild(menu);

    caixa.appendChild(entrada);
    original.parentNode.insertBefore(envolve, original);

    // O campo original vira o portador do valor. O 'required' TEM de sair:
    // campo escondido e obrigatorio faz o navegador tentar focar o que nao
    // da para ver, e o envio trava sem mensagem nenhuma.
    original.type = "hidden";
    original.removeAttribute("required");

    var dica = envolve.parentNode.querySelector(".dica");
    if (dica) { dica.textContent = "Escolha uma ou varias na lista"; }

    function sincronizar() {
      original.value = lista.map(function (c) { return c.nome; }).join(", ");
    }

    function desenhar() {
      Array.prototype.slice.call(caixa.querySelectorAll(".cidades-tag"))
        .forEach(function (t) { t.remove(); });
      lista.forEach(function (c, i) {
        var tag = document.createElement("span");
        tag.className = "cidades-tag";
        tag.textContent = bonito(c.nome) + " (" + c.uf + ")";
        var x = document.createElement("button");
        x.type = "button";
        x.className = "cidades-x";
        x.setAttribute("aria-label", "Remover " + bonito(c.nome));
        x.innerHTML =
          '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" ' +
          'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" ' +
          'aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
        x.addEventListener("click", function () {
          lista.splice(i, 1);
          sincronizar();
          desenhar();
          entrada.focus();
        });
        tag.appendChild(x);
        caixa.insertBefore(tag, entrada);
      });
      entrada.placeholder = lista.length ? "Adicionar outra" : "Digite e escolha na lista";
      sincronizar();
    }

    function jaTem(c) {
      return lista.some(function (x) {
        return normalizar(x.nome) === normalizar(c.nome) && x.uf === c.uf;
      });
    }

    function adicionar(c) {
      if (!jaTem(c)) { lista.push({ nome: c.nome, uf: c.uf }); }
      entrada.value = "";
      fechar();
      desenhar();
    }

    function fechar() {
      menu.hidden = true;
      menu.innerHTML = "";
      marcado = -1;
      entrada.setAttribute("aria-expanded", "false");
    }

    function candidatos(termo) {
      var t = normalizar(termo);
      if (t.length < 2) return [];
      // Quem comeca com o que foi digitado vem antes de quem so contem:
      // digitar "mari" deve mostrar Marilia no topo, nao Santa Maria.
      var comeca = [], contem = [];
      for (var i = 0; i < todas.length; i++) {
        var c = todas[i];
        var p = c.norm.indexOf(t);
        if (p === 0) { comeca.push(c); }
        else if (p > 0) { contem.push(c); }
        if (comeca.length >= MAX_LISTA) break;
      }
      // Dentro de cada grupo, cidade maior primeiro: quem digita "sao paulo"
      // quer a capital, nao Sao Paulo das Missoes.
      var por = function (a, b) { return b.qtd - a.qtd; };
      comeca.sort(por);
      contem.sort(por);
      return comeca.concat(contem).slice(0, MAX_LISTA);
    }

    function abrir(itens) {
      menu.innerHTML = "";
      if (!itens.length) { fechar(); return; }
      itens.forEach(function (c, i) {
        var li = document.createElement("li");
        li.className = "cidades-item";
        li.setAttribute("role", "option");
        li.id = "cidade-op-" + i;
        li.setAttribute("aria-selected", "false");
        li.innerHTML =
          '<span class="cidades-nome">' + bonito(c.nome) +
          ' <span class="cidades-uf">' + c.uf + "</span></span>" +
          '<span class="cidades-qtd">' + milhar(c.qtd) + "</span>";
        // mousedown e nao click: o blur da entrada dispara antes do click e
        // fecharia o menu debaixo do cursor.
        li.addEventListener("mousedown", function (ev) {
          ev.preventDefault();
          adicionar(c);
        });
        li.addEventListener("mouseenter", function () { destacar(i); });
        menu.appendChild(li);
      });
      menu.hidden = false;
      entrada.setAttribute("aria-expanded", "true");
      destacar(0);
    }

    function destacar(i) {
      var itens = menu.querySelectorAll(".cidades-item");
      if (!itens.length) return;
      marcado = (i + itens.length) % itens.length;
      for (var k = 0; k < itens.length; k++) {
        var on = k === marcado;
        itens[k].classList.toggle("marcado", on);
        itens[k].setAttribute("aria-selected", on ? "true" : "false");
      }
      entrada.setAttribute("aria-activedescendant", "cidade-op-" + marcado);
      itens[marcado].scrollIntoView({ block: "nearest" });
    }

    entrada.addEventListener("input", function () {
      abrir(candidatos(entrada.value));
    });

    entrada.addEventListener("keydown", function (ev) {
      var aberto = !menu.hidden;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        aberto ? destacar(marcado + 1) : abrir(candidatos(entrada.value));
      } else if (ev.key === "ArrowUp" && aberto) {
        ev.preventDefault();
        destacar(marcado - 1);
      } else if (ev.key === "Enter") {
        // Enter com menu aberto escolhe a cidade; sem menu, deixa o
        // formulario seguir. Sem este preventDefault a pagina buscaria antes
        // de a etiqueta existir.
        if (aberto && marcado >= 0) {
          ev.preventDefault();
          adicionar(candidatos(entrada.value)[marcado]);
        }
      } else if (ev.key === "Escape" && aberto) {
        ev.preventDefault();
        fechar();
      } else if (ev.key === "Backspace" && !entrada.value && lista.length) {
        lista.pop();
        desenhar();
      } else if (ev.key === "," || ev.key === ";") {
        // Quem ja tem o costume da virgula nao e punido por usa-la.
        ev.preventDefault();
        var op = candidatos(entrada.value);
        if (op.length) { adicionar(op[0]); }
      }
    });

    entrada.addEventListener("blur", function () { setTimeout(fechar, 120); });
    caixa.addEventListener("click", function () { entrada.focus(); });

    var form = original.form;
    if (form) {
      form.addEventListener("submit", function (ev) {
        // Texto digitado e nao confirmado ainda conta. Sem isto, quem digita
        // "Assis" e clica direto em Buscar recebe "escolha uma cidade" com a
        // palavra Assis na tela -- o pior tipo de recusa.
        if (entrada.value.trim()) {
          var op = candidatos(entrada.value);
          if (op.length) { adicionar(op[0]); }
        }
        if (!lista.length) {
          ev.preventDefault();
          fechar();
          entrada.focus();
          envolve.classList.add("cidades-faltando");
          setTimeout(function () {
            envolve.classList.remove("cidades-faltando");
          }, 1600);
        }
      });
    }

    // Valor que ja veio preenchido do servidor (voltar no historico, ou o
    // link de export que repete a busca) precisa virar etiqueta.
    var inicial = (original.value || "").split(",")
      .map(function (s) { return s.trim(); }).filter(Boolean);

    carregar().then(function (dados) {
      todas = dados;
      inicial.forEach(function (nome) {
        var n = normalizar(nome);
        var achado = null;
        for (var i = 0; i < todas.length; i++) {
          if (todas[i].norm === n) { achado = todas[i]; break; }
        }
        // Cidade que nao existe na base entra como etiqueta mesmo assim: o
        // servidor ja sabe reclamar dela com sugestao, e apagar em silencio
        // o que a pessoa digitou seria pior.
        lista.push(achado || { nome: nome, uf: "?" });
      });
      desenhar();
    });

    desenhar();
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.slice.call(document.querySelectorAll("input[data-cidades]"))
      .forEach(montar);
  });
})();
