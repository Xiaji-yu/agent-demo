// modal 弹窗行为断言：node tests/modal_dom_test.js <提取后的页面 JS>
// 由 tests/test_web.py::TestModalJs 调用（无 node 的环境 skip）。
// 覆盖：点蒙层不关 / ESC 脏检查 / 取消防手滑 / 确认取消恢复编辑框（带值）/ 无残留。
// 背景：页面 JS 的引号/语法错误 pytest 抓不到（实测进过一次 main），
// node --check + DOM shim 是唯一防线，故固化在此。
// DOM shim + modal 行为断言（node 运行；从 _page.js 提取 modal 块与 ESC 段执行）
const fs = require("fs");
const pageJs = fs.readFileSync(process.argv[2], "utf8");

// ---- 提取 modal 块与 ESC 段（花括号配平截取源码，不做等价重写）----
const start = pageJs.indexOf("let modalOnSave = null;");
const end = pageJs.indexOf("function field(label, html, hint) {");
if (start < 0 || end < 0) { console.error("modal block not found"); process.exit(2); }
const modalBlock = pageJs.slice(start, end);

const escIdx = pageJs.indexOf('if (e.key === "Escape"');
if (escIdx < 0) { console.error("escape handler not found"); process.exit(2); }
let depth = 0, escEnd = -1;
for (let i = pageJs.indexOf("{", escIdx); i < pageJs.length; i++) {
  if (pageJs[i] === "{") depth++;
  else if (pageJs[i] === "}") { depth--; if (depth === 0) { escEnd = i + 1; break; } }
}
const escSeg = pageJs.slice(escIdx, escEnd);

// ---- 最小 DOM shim ----
class El {
  constructor(tag, attrs) {
    this.tagName = tag; this.attrs = attrs || {};
    this.id = (attrs || {}).id || "";
    this.children = []; this.parent = null;
    this.value = (attrs || {}).value || "";
    this.type = (attrs || {}).type || (tag === "input" ? "text" : "");
    this.onclick = null; this.listeners = {};
  }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatch(t, ev) { (this.listeners[t] || []).forEach(fn => fn(ev || { target: this })); }
  remove() {
    if (this.parent) {
      const i = this.parent.children.indexOf(this);
      if (i >= 0) this.parent.children.splice(i, 1);
      this.parent = null;
    }
  }
  appendChild(c) { c.remove(); c.parent = this; this.children.push(c); return c; }
  querySelectorAll(sel) {
    const kinds = sel.split(",").map(s => s.trim().toUpperCase());
    const out = [];
    const walk = n => n.children.forEach(c => { if (kinds.includes(c.tagName)) out.push(c); walk(c); });
    walk(this); return out;
  }
}
const docShim = { body: new El("BODY", {}) };
docShim.getElementById = function (id) {
  let found = null;
  const walk = n => n.children.forEach(c => { if (c.id === id) found = c; walk(c); });
  walk(this.body); return found;
};
docShim.body.insertAdjacentHTML = function (pos, html) {
  // 扁平解析：除 wrapper 本身外所有带 id 的标签各建一个 El 挂到 mask 节点
  // （wrapper 自身即本函数创建的 mask，重复解析会造出同名内层节点，
  //  让深度优先的 getElementById 命中空节点——实测踩过）
  const mask = new El("DIV", { id: "modalMask" });
  const tagRe = /<(input|button|select|textarea)\b([^>]*)>/g;
  let m;
  while ((m = tagRe.exec(html))) {
    const attrs = {};
    const pairRe = /([\w-]+)\s*=\s*"([^"]*)"/g;
    let a; const s = m[2];
    while ((a = pairRe.exec(s))) attrs[a[1]] = a[2];
    const el = new El(m[1].toUpperCase(), attrs);
    if (el.id) mask.appendChild(el);
  }
  this.appendChild(mask);
};
let toasts = [];
globalThis.document = docShim;
globalThis.setTimeout = (fn, ms) => 0;
globalThis.toast = (msg, kind) => { toasts.push(msg); };
globalThis.esc = s => String(s);

eval(modalBlock);
const escHandler = (e) => { eval(escSeg); };

function modalOpen() { return !!docShim.getElementById("modalMask"); }
function inputSet(id, v) { docShim.getElementById(id).value = v; }
let pass = 0, fail = 0;
function check(name, cond) {
  if (cond) { pass++; console.log("  ok  " + name); }
  else { fail++; console.log("FAIL  " + name); }
}

(async () => {
  // 1) openModal → 点蒙层不关闭
  openModal("编辑测试", '<input id="f1" class="mfield__input" value="a">', async () => {});
  check("1a openModal 后弹窗在场", modalOpen());
  docShim.getElementById("modalMask").dispatch("click", { target: docShim.getElementById("modalMask") });
  check("1b 点蒙层不关闭", modalOpen());

  // 2) 有修改 → ESC 不关；无修改 → ESC 关
  inputSet("f1", "改过的值");
  escHandler({ key: "Escape" });
  check("2a 有修改 ESC 不关闭", modalOpen());
  check("2b ESC 提示了未保存", toasts.some(t => t.includes("未保存")));
  docShim.getElementById("f1").value = "a"; // 改回原值 → 干净
  escHandler({ key: "Escape" });
  check("2c 无修改 ESC 关闭", !modalOpen());

  // 3) 取消防手滑：第一次警告不关，第二次才关
  openModal("编辑测试", '<input id="f1" class="mfield__input" value="a">', async () => {});
  inputSet("f1", "用户填了半天");
  closeModalGuarded();
  check("3a 有修改第一次取消不关", modalOpen());
  check("3b 提示了再点一次", toasts.some(t => t.includes("再点一次")));
  closeModalGuarded();
  check("3c 第二次取消真关", !modalOpen());

  // 4) 确认框取消 → 编辑框带原值恢复
  openModal("编辑测试", '<input id="f1" class="mfield__input" value="初始">', async () => {});
  inputSet("f1", "填了重要配置");
  const p = confirmDialog("确认写入", "old → new", "确认");
  check("4a 确认框替换编辑框（暂存）",
    docShim.getElementById("modalOk") !== null && docShim.getElementById("f1") === null);
  closeModal(); // 确认框的「取消」按钮
  check("4b 取消确认后编辑框恢复", modalOpen() && docShim.getElementById("f1") !== null);
  check("4c 恢复后值还在", docShim.getElementById("f1").value === "填了重要配置");

  // 5) 恢复后的框是脏的：取消需两次
  closeModalGuarded();
  check("5a 恢复框第一次取消不关", modalOpen());
  closeModalGuarded();
  check("5b 恢复框第二次取消关闭", !modalOpen());

  // 6) 确认 OK → 恢复编辑框（等结果）→ 动作成功 closeModal 收尾
  openModal("编辑测试", '<input id="f1" class="mfield__input" value="x">', async () => {});
  inputSet("f1", "y");
  const p2 = confirmDialog("确认写入", "old → new", "确认");
  docShim.getElementById("modalOk").onclick();
  check("6a 确认后编辑框在场（带值）", modalOpen() && docShim.getElementById("f1").value === "y");
  const confirmed = await p2;
  check("6b confirmDialog resolve(true)", confirmed === true);
  closeModal(); // 调用方成功路径
  check("6c 成功收尾全关", !modalOpen());

  // 7) 无编辑框时确认框取消 → 直接关、无残留
  const p3 = confirmDialog("独立确认", "内容", "确认");
  closeModal();
  const cancelled = await p3;
  check("7a 独立确认取消 resolve(false)", cancelled === false);
  check("7b 无残留弹窗", !modalOpen());

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
