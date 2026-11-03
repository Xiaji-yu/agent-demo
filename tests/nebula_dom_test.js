// 记忆星云的多指手势断言：node tests/nebula_dom_test.js <提取后的页面 JS>
// 由 tests/test_web.py::TestNebulaJs 调用（无 node 的环境 skip）。
// 覆盖移动端双指缩放的真实回归面：单指旋转 / 双指按距离比缩放 / 捏合不算点击 /
// 松开一指后重新锚定 / pointercancel 清理。
const fs = require("fs");
const page = fs.readFileSync(process.argv[2], "utf8");

function extractFn(name) {
  const i = page.indexOf("function " + name + "(");
  if (i < 0) throw new Error("missing function " + name);
  const b = page.indexOf("{", i);
  let depth = 0;
  for (let j = b; j < page.length; j++) {
    const c = page[j];
    if (c === "{") depth++;
    else if (c === "}") { depth--; if (depth === 0) return page.slice(i, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

const constMatch = page.match(/const NEB_ZOOM_MIN = ([0-9.]+), NEB_ZOOM_MAX = ([0-9.]+);/);
if (!constMatch) { console.error("zoom constants not found"); process.exit(2); }

let fails = 0, passes = 0;
function check(name, cond, extra) {
  if (cond) { passes++; console.log("ok   " + name); }
  else { fails++; console.log("FAIL " + name + (extra ? " :: " + extra : "")); }
}

let nebState = {
  rotY: 0, rotX: 0, zoom: 1, drag: null, moved: 0,
  pointers: new Map(), pinch: null
};
let clicks = 0, redraws = 0, hovers = 0;
function nebRedraw() { redraws++; }
function nebHover() { hovers++; }
function nebClick() { clicks++; }
function nebLeave() {}

const handlers = {};
const cv = {
  dataset: {},
  addEventListener(t, fn) { (handlers[t] = handlers[t] || []).push(fn); },
  setPointerCapture() {},
};
const src = [
  "const NEB_ZOOM_MIN = " + constMatch[1] + ", NEB_ZOOM_MAX = " + constMatch[2] + ";",
  extractFn("nebClampZoom"),
  extractFn("nebPinchZoom"),
  extractFn("nebDist"),
  extractFn("nebReanchor"),
  extractFn("nebBind"),
].join("\n");
eval(src + "\nnebBind(cv);");

function fire(type, ev) { (handlers[type] || []).forEach((fn) => fn(ev)); }
function ev(id, x, y) {
  return { pointerId: id, clientX: x, clientY: y, pointerType: "touch" };
}
function reset() {
  nebState.rotY = 0; nebState.rotX = 0; nebState.zoom = 1;
  nebState.drag = null; nebState.moved = 0; nebState.pinch = null;
  nebState.pointers.clear();
  clicks = 0; redraws = 0;
}

// 1. 单指拖动仍然旋转
reset();
fire("pointerdown", ev(1, 100, 100));
fire("pointermove", ev(1, 200, 100));
check("单指拖动旋转", nebState.rotY > 0.5, "rotY=" + nebState.rotY);
fire("pointerup", ev(1, 200, 100));

// 2. 双指按距离比缩放，且不旋转、不算点击
reset();
fire("pointerdown", ev(2, 100, 100));
fire("pointerdown", ev(3, 200, 100));
fire("pointermove", ev(3, 300, 100));
check("双指捏合放大一倍", Math.abs(nebState.zoom - 2) < 1e-6, "zoom=" + nebState.zoom);
check("捏合不旋转", nebState.rotY === 0, "rotY=" + nebState.rotY);
fire("pointerup", ev(3, 300, 100));
fire("pointerup", ev(2, 100, 100));
check("捏合不算点击", clicks === 0, "clicks=" + clicks);

// 3. 缩放有上下限
reset();
fire("pointerdown", ev(4, 0, 0));
fire("pointerdown", ev(5, 10, 0));
fire("pointermove", ev(5, 2000, 0));
check("放大触顶", Math.abs(nebState.zoom - 3.4) < 1e-6, "zoom=" + nebState.zoom);
fire("pointerup", ev(5, 2000, 0));
fire("pointerup", ev(4, 0, 0));

// 4. 轻点仍然可点击，拖动不触发点击
reset();
fire("pointerdown", ev(6, 0, 0));
fire("pointerup", ev(6, 0, 0));
check("轻点可点击", clicks === 1, "clicks=" + clicks);
reset();
fire("pointerdown", ev(7, 0, 0));
fire("pointermove", ev(7, 40, 0));
fire("pointerup", ev(7, 40, 0));
check("拖动不触发点击", clicks === 0, "clicks=" + clicks);

// 5. 双指中途 cancel：回到单指拖拽状态，不留 pinch
reset();
fire("pointerdown", ev(8, 0, 0));
fire("pointerdown", ev(9, 100, 0));
fire("pointercancel", ev(9, 100, 0));
check("cancel 后回到单指", nebState.pointers.size === 1 && nebState.pinch === null);
fire("pointerup", ev(8, 0, 0));

console.log(passes + " passed, " + fails + " failed");
process.exit(fails ? 1 : 0);
