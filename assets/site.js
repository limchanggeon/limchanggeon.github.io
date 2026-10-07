/* 모든 페이지 공용: 테마, 모바일 메뉴, 스크롤 표시, 추적 박스, 이메일 복사 */
(function () {
  var root = document.documentElement;
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  window.site = { reduce: reduce };

  /* ── theme ── */
  var themeBtn = document.getElementById('theme-btn');
  function isDark() {
    var t = root.getAttribute('data-theme');
    return t ? t === 'dark' : !!(window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
  }
  function syncIcon() {
    if (!themeBtn) return;
    themeBtn.querySelector('use').setAttribute('href', isDark() ? '#i-sun' : '#i-moon');
    themeBtn.setAttribute('aria-label', isDark() ? '라이트 모드로 전환' : '다크 모드로 전환');
  }
  try { var saved = localStorage.getItem('cgl-theme'); if (saved) root.setAttribute('data-theme', saved); } catch (e) {}
  syncIcon();
  if (themeBtn) themeBtn.addEventListener('click', function () {
    var next = isDark() ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('cgl-theme', next); } catch (e) {}
    syncIcon();
  });

  /* ── mobile drawer ── */
  var menuBtn = document.getElementById('menu-btn'), drawer = document.getElementById('drawer');
  function setDrawer(open) {
    drawer.hidden = !open;
    menuBtn.setAttribute('aria-expanded', String(open));
    menuBtn.setAttribute('aria-label', open ? '메뉴 닫기' : '메뉴 열기');
    menuBtn.querySelector('use').setAttribute('href', open ? '#i-close' : '#i-menu');
  }
  if (menuBtn && drawer) {
    menuBtn.addEventListener('click', function () { setDrawer(drawer.hidden); });
    drawer.addEventListener('click', function (e) { if (e.target.closest('a')) setDrawer(false); });
  }

  /* ── scroll spy (섹션이 있는 페이지만) ── */
  var spyLinks = document.querySelectorAll('[data-spy]');
  function activate(id) { spyLinks.forEach(function (a) { a.classList.toggle('active', a.getAttribute('data-spy') === id); }); }
  var secs = document.querySelectorAll('main > .sec[id]');
  if (secs.length) {
    activate(secs[0].id);
    if ('IntersectionObserver' in window) {
      var io = new IntersectionObserver(function (es) { es.forEach(function (en) { if (en.isIntersecting) activate(en.target.id); }); }, { rootMargin: '-35% 0px -60% 0px' });
      secs.forEach(function (s) { io.observe(s); });
    }
  }

  /* ── progress bar + to-top ── */
  var progress = document.getElementById('progress'), toTop = document.getElementById('to-top');
  function onScroll() {
    var max = root.scrollHeight - innerHeight;
    if (progress) progress.style.transform = 'scaleX(' + (max > 0 ? scrollY / max : 0) + ')';
    if (toTop) toTop.hidden = scrollY < 600;
  }
  addEventListener('scroll', onScroll, { passive: true }); onScroll();
  if (toTop) toTop.addEventListener('click', function () { scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' }); });

  /* ── tracking box: 마우스를 올린 카드를 탐지 박스처럼 따라간다 (동적으로 그린 카드 포함) ── */
  var tracker = document.getElementById('tracker');
  if (tracker && window.matchMedia && matchMedia('(hover: hover) and (pointer: fine)').matches) {
    var tLabel = tracker.querySelector('span'), current = null;
    document.addEventListener('mouseover', function (e) {
      var el = e.target.closest && e.target.closest('.track');
      if (el === current) return;
      current = el;
      if (!el) { tracker.classList.remove('on'); return; }
      var r = el.getBoundingClientRect(), pad = 4;
      tracker.style.left = (r.left + scrollX - pad) + 'px';
      tracker.style.top = (r.top + scrollY - pad) + 'px';
      tracker.style.width = (r.width + pad * 2) + 'px';
      tracker.style.height = (r.height + pad * 2) + 'px';
      tLabel.textContent = el.getAttribute('data-cls') + ' ' + (el.getAttribute('data-conf') || (0.86 + Math.random() * 0.13).toFixed(2));
      tracker.classList.add('on');
    });
    document.addEventListener('toggle', function () { current = null; tracker.classList.remove('on'); }, true);
  }

  /* ── toast + copy email ── */
  var toast = document.getElementById('toast'), toastTimer;
  function showToast(msg) {
    if (!toast) return;
    toast.textContent = msg; toast.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { toast.hidden = true; }, 2000);
  }
  window.site.toast = showToast;
  function selectText(text) {
    var el = document.querySelector('[data-copy-target]') || document.getElementById('side-email');
    if (el) {
      var r = document.createRange(); r.selectNodeContents(el);
      var s = getSelection(); s.removeAllRanges(); s.addRange(r);
    }
    showToast(text + ' · ⌘C 또는 Ctrl+C로 복사하세요');
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-copy]');
    if (!b) return;
    var v = b.getAttribute('data-copy');
    try { navigator.clipboard.writeText(v).then(function () { showToast('이메일을 복사했어요'); }, function () { selectText(v); }); }
    catch (err) { selectText(v); }
  });

  /* ── 브라우저 저장소: 관심 기업·자소서처럼 내 브라우저에 남길 데이터 ── */
  window.site.store = {
    get: function (key, fallback) {
      try { var v = localStorage.getItem('cgl-' + key); return v ? JSON.parse(v) : fallback; } catch (e) { return fallback; }
    },
    set: function (key, value) {
      try { localStorage.setItem('cgl-' + key, JSON.stringify(value)); return true; } catch (e) { return false; }
    }
  };

  /* ── API 호출: 서버가 비밀번호를 요구하면 로그인 창을 띄우고 다시 시도한다 ── */
  var loginWaiters = null;
  function askPassword() {
    if (loginWaiters) return new Promise(function (res, rej) { loginWaiters.push([res, rej]); });
    loginWaiters = [];
    return new Promise(function (resolve, reject) {
      loginWaiters.push([resolve, reject]);
      var wrap = document.createElement('div');
      wrap.className = 'login-wrap';
      wrap.innerHTML = '<form class="login" role="dialog" aria-modal="true" aria-labelledby="login-h">' +
        '<span class="login-mark" aria-hidden="true"></span>' +
        '<h2 id="login-h">비밀번호를 입력해 주세요</h2>' +
        '<p>기업 분석과 자소서 작성은 제 API 키를 쓰는 기능이라 잠가 두었어요.</p>' +
        '<label for="login-pw" class="sr">비밀번호</label>' +
        '<input class="field" id="login-pw" type="password" autocomplete="current-password" placeholder="비밀번호" required>' +
        '<p class="login-err" id="login-err" hidden></p>' +
        '<div class="btn-row"><a class="btn" href="index.html">포트폴리오로 돌아가기</a><button class="btn primary" type="submit">확인</button></div></form>';
      document.body.appendChild(wrap);
      var input = wrap.querySelector('#login-pw'), err = wrap.querySelector('#login-err');
      input.focus();
      wrap.querySelector('form').addEventListener('submit', function (e) {
        e.preventDefault();
        fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: input.value }) })
          .then(function (r) {
            if (!r.ok) { err.textContent = '비밀번호가 맞지 않아요. 다시 입력해 주세요.'; err.hidden = false; input.select(); return; }
            wrap.remove();
            var ws = loginWaiters; loginWaiters = null;
            ws.forEach(function (w) { w[0](); });
          })
          .catch(function () { err.textContent = '서버에 연결하지 못했어요. 잠시 뒤 다시 시도해 주세요.'; err.hidden = false; });
      });
    });
  }
  window.site.api = function api(path, opts) {
    return fetch(path, Object.assign({ credentials: 'same-origin' }, opts)).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (r.status === 401 && d.auth) return askPassword().then(function () { return api(path, opts); });
        if (!r.ok) throw new Error(d.error || ('서버 응답 ' + r.status));
        return d;
      });
    });
  };
})();
