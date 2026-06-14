/**
 * Mermaid Pan & Zoom — 通用增强脚本
 *
 * 功能：给所有 .diagram-container 内的 mermaid 图加上
 *   - 滚轮缩放（以光标为中心）
 *   - 拖拽移动
 *   - 工具栏（+ / − / 重置 + 缩放百分比）
 *   - 双击 fit 到容器
 *   - 初始自动 fit
 *
 * 设计：用 MutationObserver 监听 svg 出现，兼容任意 mermaid 初始化方式
 *      （startOnLoad、手动 mermaid.run、tab 切换后动态渲染等）。
 *      不修改 mermaid 配置，仅通过 CSS + transform 增强。
 *
 * 用法：在 HTML 的 </body> 前加 <script src="mermaid-panzoom.js"></script>
 */
(function () {
  'use strict';
  if (window.__mermaidPanzoomReady) return;
  window.__mermaidPanzoomReady = true;

  /* ===== 样式注入（覆盖各页面原有的 diagram-container 规则） ===== */
  var css = [
    '.diagram-container { min-height: 180px !important; padding: 2rem !important; overflow: hidden !important; position: relative !important; user-select: none !important; }',
    '.diagram-container > .diagram-label { top: 1rem !important; left: 1rem !important; right: auto !important; z-index: 3 !important; pointer-events: none !important; }',
    '.diagram-container pre { margin: 0 !important; }',
    '.diagram-container pre.mermaid { display: flex !important; align-items: center !important; justify-content: center !important; min-height: 140px !important; cursor: grab !important; overflow: hidden !important; }',
    '.diagram-container pre.mermaid:active, .diagram-container pre.mermaid.pz-dragging { cursor: grabbing !important; }',
    '.diagram-container pre.mermaid svg { transform-origin: center center !important; will-change: transform !important; overflow: visible !important; max-width: none !important; height: auto !important; }',
    '.pz-toolbar { position: absolute; top: 1rem; right: 1rem; z-index: 5; display: flex; align-items: center; gap: 0.375rem; background: var(--bg-card, #1a1a2e); border: 1px solid var(--border, #2a2a4a); border-radius: 10px; padding: 0.3rem; }',
    '.pz-toolbar button { width: 28px; height: 28px; border-radius: 7px; border: none; background: rgba(255,255,255,0.05); color: var(--text-secondary, #9999b8); cursor: pointer; font-size: 1rem; line-height: 1; font-family: inherit; transition: background 0.15s; }',
    '.pz-toolbar button:hover { background: rgba(79,143,247,0.25); color: var(--text-primary, #e8e8f0); }',
    '.pz-badge { font-size: 0.7rem; color: var(--text-muted, #666688); font-family: monospace; min-width: 38px; text-align: center; }',
    '.pz-hint { font-size: 0.65rem; color: var(--text-muted, #666688); padding: 0 0.5rem; border-left: 1px solid var(--border, #2a2a4a); margin-left: 0.25rem; white-space: nowrap; }'
  ].join('\n');
  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  var MIN_SCALE = 0.3, MAX_SCALE = 4;
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

  function buildToolbar(container, handlers) {
    if (container.querySelector('.pz-toolbar')) return container.querySelector('.pz-badge');
    var bar = document.createElement('div');
    bar.className = 'pz-toolbar';
    bar.innerHTML =
      '<button type="button" title="放大">+</button>' +
      '<button type="button" title="缩小">−</button>' +
      '<button type="button" title="重置">⟲</button>' +
      '<span class="pz-badge">100%</span>' +
      '<span class="pz-hint">滚轮缩放 · 拖拽移动 · 双击重置</span>';
    container.appendChild(bar);
    var btns = bar.querySelectorAll('button');
    btns[0].addEventListener('click', function (e) { e.stopPropagation(); handlers.zoomIn(); });
    btns[1].addEventListener('click', function (e) { e.stopPropagation(); handlers.zoomOut(); });
    btns[2].addEventListener('click', function (e) { e.stopPropagation(); handlers.reset(); });
    bar.addEventListener('mousedown', function (e) { e.stopPropagation(); });
    bar.addEventListener('dblclick', function (e) { e.stopPropagation(); });
    bar.addEventListener('wheel', function (e) { e.stopPropagation(); });
    return bar.querySelector('.pz-badge');
  }

  function attach(svg) {
    if (svg.dataset.pzReady) return;
    svg.dataset.pzReady = '1';
    var wrapper = svg.parentElement;
    var container = wrapper.closest('.diagram-container');
    if (!container) return;

    var st = { scale: 1, x: 0, y: 0 };
    var dragging = false, lastX = 0, lastY = 0;

    function apply(animate) {
      svg.style.transition = animate ? 'transform 0.18s ease-out' : 'none';
      svg.style.transform = 'translate(' + st.x + 'px,' + st.y + 'px) scale(' + st.scale + ')';
      if (badge) badge.textContent = Math.round(st.scale * 100) + '%';
    }

    // fit：只 fit 宽度（图比容器宽时缩到适配），高度由容器自然撑开
    function fit() {
      var prev = svg.style.transform;
      svg.style.transform = 'none';
      var raw = svg.getBoundingClientRect();
      svg.style.transform = prev;
      var svgW = raw.width;
      var cW = container.clientWidth - 64; // 减去 padding（左右各 2rem）
      if (!svgW) { st.scale = 1; st.x = 0; st.y = 0; apply(true); return; }
      var f = cW > 0 ? Math.min(cW / svgW, 1) : 1;
      if (!isFinite(f) || f <= 0) f = 1;
      st.scale = f; st.x = 0; st.y = 0; apply(true);
    }

    var badge = buildToolbar(container, {
      zoomIn: function () { st.scale = clamp(st.scale * 1.2, MIN_SCALE, MAX_SCALE); apply(true); },
      zoomOut: function () { st.scale = clamp(st.scale * 0.83, MIN_SCALE, MAX_SCALE); apply(true); },
      reset: function () { fit(); }
    });

    // 滚轮缩放（以光标为中心）
    wrapper.addEventListener('wheel', function (e) {
      e.preventDefault();
      var rect = svg.getBoundingClientRect();
      var cx = e.clientX - (rect.left + rect.width / 2);
      var cy = e.clientY - (rect.top + rect.height / 2);
      var delta = e.deltaY < 0 ? 1.12 : 0.89;
      var ns = clamp(st.scale * delta, MIN_SCALE, MAX_SCALE);
      var ratio = ns / st.scale;
      st.x = cx - (cx - st.x) * ratio;
      st.y = cy - (cy - st.y) * ratio;
      st.scale = ns;
      apply(false);
    }, { passive: false });

    // 拖拽移动
    wrapper.addEventListener('mousedown', function (e) {
      if (e.button !== 0) return;
      dragging = true; lastX = e.clientX; lastY = e.clientY;
      wrapper.classList.add('pz-dragging');
      e.preventDefault();
    });
    window.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      st.x += e.clientX - lastX; st.y += e.clientY - lastY;
      lastX = e.clientX; lastY = e.clientY;
      apply(false);
    });
    window.addEventListener('mouseup', function () {
      if (dragging) { dragging = false; wrapper.classList.remove('pz-dragging'); }
    });

    // 双击 fit
    wrapper.addEventListener('dblclick', function () { fit(); });

    // 初始 fit（延迟到布局稳定）
    setTimeout(fit, 60);
  }

  function scan() {
    document.querySelectorAll('pre.mermaid svg, .mermaid svg').forEach(attach);
  }

  // MutationObserver：mermaid 异步渲染出 svg 后自动绑定（兼容 tab 切换动态渲染）
  var observer = new MutationObserver(function () { scan(); });
  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // 多时机兜底扫描
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(scan, 300); });
  } else {
    setTimeout(scan, 300);
  }
  window.addEventListener('load', function () { setTimeout(scan, 800); });
})();
