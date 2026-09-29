/* 录制用户在页面上的操作。
 *
 * 由插件通过 Page.addScriptToEvaluateOnNewDocument 注入，所以**每次新页面加载都会重新注入**
 * —— 用户点了链接跳走之后，录制还在继续。
 *
 * 记录存在 sessionStorage 里（同一个站内跨页面能读回来）。跨站跳转时会丢，
 * 那是这个方案的已知边界 —— 见 main.py 里 record_dump 的说明。
 */
(() => {
  if (window.__myautoworkRecording) return 'already';

  const KEY = '__myautowork_log';
  const MAX = 500;

  /* **内存是主，sessionStorage 只是镜像。**
   *
   * 一开始我让 sessionStorage 当唯一存储，结果 `about:blank` 上什么都录不到 ——
   * 那个页面的 origin 是**不透明的**，访问 sessionStorage 会直接抛 SecurityError。
   * 隐私模式下也一样。捕获了异常之后读回来永远是空数组，而现象是"点了开始录制，
   * 操作了半天，一步都没录到"，看不出跟存储权限有任何关系。
   *
   * 内存这份在**当前页面**上永远可用；sessionStorage 那份负责跨页面导航之后还能读回来。
   */
  let memory = null;

  const read = () => {
    if (memory) return memory;
    try {
      memory = JSON.parse(sessionStorage.getItem(KEY) || '[]');
    } catch (e) {
      memory = []; // 不透明 origin / 隐私模式：只能靠内存
    }
    return memory;
  };

  const write = (rows) => {
    memory = rows;
    try {
      sessionStorage.setItem(KEY, JSON.stringify(rows.slice(-MAX)));
    } catch (e) {
      /* 存不进去也不影响当前页面继续录 */
    }
  };

  const push = (row) => {
    const rows = read();
    rows.push(row);
    write(rows);
  };

  /* 给一个元素算出一个**稳定的**选择器。
   *
   * 这个函数是整套录制里最关键的部分。随便生成一个能选中的选择器很容易
   * （一路 nth-child 到底就行），但那种选择器改一次页面结构就失效了 ——
   * 录出来的东西过两天就不能用，等于白录。
   *
   * 优先级从"最稳"到"最不稳"：
   *   1. id（而且页面上唯一）
   *   2. data-testid / name / aria-label 这类**语义属性**（改版时最不容易动）
   *   3. 从最近的带 id 的祖先往下，逐级 nth-of-type
   */
  const cssPath = (el) => {
    if (!el || el.nodeType !== 1) return '';
    const esc = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s);
    const unique = (sel) => {
      try {
        return document.querySelectorAll(sel).length === 1;
      } catch (e) {
        return false;
      }
    };

    if (el.id) {
      const sel = '#' + esc(el.id);
      if (unique(sel)) return sel;
    }

    for (const attr of ['data-testid', 'data-test', 'data-cy', 'name', 'aria-label', 'placeholder']) {
      const value = el.getAttribute && el.getAttribute(attr);
      if (!value) continue;
      const sel = `${el.tagName.toLowerCase()}[${attr}="${value.replace(/"/g, '\\"')}"]`;
      if (unique(sel)) return sel;
    }

    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (!parent) {
        parts.unshift(tag);
        break;
      }
      const same = [...parent.children].filter((c) => c.tagName === node.tagName);
      const index = same.indexOf(node) + 1;
      parts.unshift(same.length > 1 ? `${tag}:nth-of-type(${index})` : tag);

      if (parent.id && unique('#' + esc(parent.id))) {
        parts.unshift('#' + esc(parent.id));
        break;
      }
      node = parent;
    }
    return parts.join(' > ');
  };

  const describe = (el) => {
    const text = (el.innerText || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    return { tag: el.tagName.toLowerCase(), text };
  };

  const pick = (target) => {
    if (!target || target.nodeType !== 1) return target;
    return (
      target.closest('a,button,input,select,textarea,label,[role="button"],[onclick]') || target
    );
  };

  document.addEventListener(
    'click',
    (event) => {
      // **停录之后不能继续记。** 只翻标志位是不够的 —— 监听器本身还得看一眼，
      // 否则"停止录制"只是名义上的，它一直在往列表里加。
      if (!window.__myautoworkRecording) return;
      const el = pick(event.target);
      if (!el || el.nodeType !== 1) return;
      push({ type: 'click', selector: cssPath(el), ...describe(el) });
    },
    true,
  );

  /* 用 change 而不是 input。input 每敲一个键就触发一次，录出来是
   * "输入 a"、"输入 ab"、"输入 abc" 三条；change 在离开输入框时触发一次，
   * 拿到的是最终值 —— 那才是用户想做的事。 */
  document.addEventListener(
    'change',
    (event) => {
      if (!window.__myautoworkRecording) return;
      const el = event.target;
      if (!el || !/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      push({ type: 'input', selector: cssPath(el), value: String(el.value ?? ''), ...describe(el) });
    },
    true,
  );

  window.__myautoworkRecording = true;
  window.__myautoworkRead = read;
  window.__myautoworkClear = () => write([]);
  return 'ok';
})();
