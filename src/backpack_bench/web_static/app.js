"use strict";

const state = {
  suites: [],
  suiteId: null,
  scenarioId: null,
  detail: null,
  selectedItemId: null,
  selectedRotation: 0,
  placements: new Map(),
  draggingItemId: null,
  dragOffset: [0, 0],
  dragAnchor: null,
  dragGrabIndex: null,
  dragGhost: null,
  pointerDrag: null,
  suppressClickUntil: 0,
  suppressContextMenuUntil: 0,
  evaluationVersion: 0,
  runConfigs: [],
  runConfigId: null,
  runPreview: null,
  apiHistory: [],
  apiHistoryId: null,
  apiSaveTimer: null,
  selectedRunId: null,
  pollTimer: null,
  runEventSource: null,
  runEventRunId: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const CATEGORY_LABELS = { weapon: "武器", support: "辅助" };
const STAT_LABELS = { attack: "攻击" };
const RUN_STATUS_LABELS = {
  pending: "待运行",
  starting: "启动中",
  running: "运行中",
  stopping: "中断中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
};
const API_HISTORY_STORAGE_KEY = "bbbench.api-history.v1";

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const message = typeof body === "object" && body?.detail ? body.detail : String(body);
    throw new Error(message || `HTTP ${response.status}`);
  }
  return body;
}

function setServerState(kind, text) {
  const node = $("#server-state");
  node.classList.toggle("is-online", kind === "online");
  node.classList.toggle("is-error", kind === "error");
  node.querySelector("span:last-child").textContent = text;
}

function setupTabs() {
  $$(".tab-button").forEach((button) => {
    button.addEventListener("click", () => {
      const tab = button.dataset.tab;
      $$(".tab-button").forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-selected", String(active));
      });
      $("#lab-panel").hidden = tab !== "lab";
      $("#runs-panel").hidden = tab !== "runs";
      if (tab === "runs" && !state.runConfigs.length) loadRunConfigs();
    });
  });
}

function currentSuite() {
  return state.suites.find((suite) => suite.id === state.suiteId) || null;
}

function currentScenarioSummary() {
  return currentSuite()?.scenarios.find((scenario) => scenario.id === state.scenarioId) || null;
}

function instanceById(itemId) {
  return state.detail?.instances.find((item) => item.item_id === itemId) || null;
}

function rotatedShapeWithIndices(shape, rotation) {
  const turns = ((rotation % 360) + 360) % 360 / 90;
  const rotated = shape.map(([sourceRow, sourceCol], index) => {
    let row = sourceRow;
    let col = sourceCol;
    for (let turn = 0; turn < turns; turn += 1) [row, col] = [col, -row];
    return [row, col, index];
  });
  const minRow = Math.min(...rotated.map(([row]) => row));
  const minCol = Math.min(...rotated.map(([, col]) => col));
  return rotated.map(([row, col, index]) => [row - minRow, col - minCol, index]);
}

function rotatedShape(shape, rotation) {
  return rotatedShapeWithIndices(shape, rotation).map(([row, col]) => [row, col]);
}

function occupiedCells(instance, placement) {
  return rotatedShape(instance.shape, placement.rotation).map(([row, col]) => [
    placement.row + row,
    placement.col + col,
  ]);
}

function rotateVector(vector, rotation) {
  let [row, col] = vector;
  const turns = ((rotation % 360) + 360) % 360 / 90;
  for (let turn = 0; turn < turns; turn += 1) [row, col] = [col, -row];
  return [row, col];
}

function effectPreview(instance, candidate) {
  const valid = new Set(state.detail.valid_cells.map(([row, col]) => `${row},${col}`));
  const owners = new Map();
  for (const placement of state.placements.values()) {
    if (placement.item_id === candidate.item_id) continue;
    const other = instanceById(placement.item_id);
    if (!other) continue;
    occupiedCells(other, placement).forEach(([row, col]) => {
      const key = `${row},${col}`;
      if (!owners.has(key)) owners.set(key, placement.item_id);
    });
  }
  const range = new Map();
  const targets = new Set();
  let supportedEffects = 0;
  const sourceCells = occupiedCells(instance, candidate);
  const sourceKeys = new Set(sourceCells.map(([row, col]) => `${row},${col}`));

  function rangeEntry(row, col) {
    const key = `${row},${col}`;
    if (!valid.has(key) || sourceKeys.has(key)) return null;
    if (!range.has(key)) range.set(key, { row, col, bonuses: new Map() });
    return range.get(key);
  }

  function markTarget(entry, ownerId, config, seenTargets) {
    if (!entry || !ownerId) return;
    const target = instanceById(ownerId);
    const targetCategory = config.target_category || "weapon";
    if (!target || target.category !== targetCategory) return;
    const oncePerTarget = config.once_per_target !== false;
    if (oncePerTarget && seenTargets.has(ownerId)) return;
    if (oncePerTarget) seenTargets.add(ownerId);
    targets.add(ownerId);
    const stat = config.stat || "attack";
    const amount = Number(config.amount ?? 0);
    entry.bonuses.set(stat, (entry.bonuses.get(stat) || 0) + amount);
  }

  for (const effect of instance.effects || []) {
    const seenTargets = new Set();
    if (effect.type === "adjacent_stat_bonus") {
      supportedEffects += 1;
      const config = {
        directions: [[0, -1], [0, 1]],
        target_category: "weapon",
        stat: "attack",
        amount: 1,
        once_per_target: true,
        ...(effect.config || {}),
      };
      const directions = config.directions;
      for (const [sourceRow, sourceCol] of sourceCells) {
        for (const direction of directions) {
          const [deltaRow, deltaCol] = rotateVector(direction, candidate.rotation);
          const row = sourceRow + deltaRow;
          const col = sourceCol + deltaCol;
          const entry = rangeEntry(row, col);
          markTarget(entry, owners.get(`${row},${col}`), config, seenTargets);
        }
      }
    } else if (effect.type === "ray_stat_bonus") {
      supportedEffects += 1;
      const config = {
        direction: [-1, 0],
        target_category: "weapon",
        stat: "attack",
        amount: 4,
        once_per_target: true,
        blocked: false,
        ...(effect.config || {}),
      };
      const [deltaRow, deltaCol] = rotateVector(
        config.direction,
        candidate.rotation,
      );
      for (const [sourceRow, sourceCol] of sourceCells) {
        let row = sourceRow + deltaRow;
        let col = sourceCol + deltaCol;
        while (valid.has(`${row},${col}`)) {
          const key = `${row},${col}`;
          const ownerId = owners.get(key);
          const entry = rangeEntry(row, col);
          markTarget(entry, ownerId, config, seenTargets);
          if (ownerId && config.blocked === true) break;
          row += deltaRow;
          col += deltaCol;
        }
      }
    }
  }
  return { range, targets, supportedEffects };
}

function renderEffectPreview(instance, candidate) {
  const preview = effectPreview(instance, candidate);
  for (const entry of preview.range.values()) {
    const cell = document.querySelector(
      `.board-cell[data-row="${entry.row}"][data-col="${entry.col}"]`,
    );
    if (!cell) continue;
    cell.classList.add("is-effect-range");
    const marker = document.createElement("span");
    marker.className = "effect-marker";
    const bonuses = Array.from(entry.bonuses.entries());
    if (bonuses.length) {
      cell.classList.add("is-effect-target");
      marker.classList.add("is-target");
      marker.textContent = bonuses
        .map(([, amount]) => `${amount >= 0 ? "+" : ""}${amount}`)
        .join("/");
      marker.title = bonuses
        .map(([stat, amount]) => `${STAT_LABELS[stat] || stat} ${amount >= 0 ? "+" : ""}${amount}`)
        .join("，");
    } else {
      marker.textContent = "✦";
      marker.title = "效果范围";
    }
    marker.setAttribute("aria-hidden", "true");
    cell.append(marker);
  }
  for (const targetId of preview.targets) {
    const target = instanceById(targetId);
    const placement = state.placements.get(targetId);
    if (!target || !placement) continue;
    occupiedCells(target, placement).forEach(([row, col]) => {
      document.querySelector(`.board-cell[data-row="${row}"][data-col="${col}"]`)
        ?.classList.add("is-effect-target");
    });
  }
  return preview;
}

function createDragGhost(instance, rotation) {
  const shape = rotatedShape(instance.shape, rotation);
  const height = Math.max(...shape.map(([row]) => row)) + 1;
  const width = Math.max(...shape.map(([, col]) => col)) + 1;
  const occupied = new Set(shape.map(([row, col]) => `${row},${col}`));
  const ghost = document.createElement("div");
  ghost.className = `drag-ghost ${instance.category === "weapon" ? "is-weapon" : "is-support"}`;
  ghost.style.setProperty("--ghost-cols", width);
  for (let row = 0; row < height; row += 1) {
    for (let col = 0; col < width; col += 1) {
      const cell = document.createElement("i");
      if (occupied.has(`${row},${col}`)) cell.className = "is-filled";
      ghost.append(cell);
    }
  }
  document.body.append(ghost);
  return ghost;
}

function positionDragGhost(clientX, clientY) {
  if (!state.dragGhost) return;
  const [offsetRow, offsetCol] = state.dragOffset;
  state.dragGhost.style.left = `${clientX - (offsetCol * 27 + 18)}px`;
  state.dragGhost.style.top = `${clientY - (offsetRow * 27 + 18)}px`;
}

function armPointerDrag(itemId, offset, event) {
  if (event.button !== 0) return;
  const instance = instanceById(itemId);
  if (!instance) return;
  const existing = state.placements.get(itemId);
  const rotation = existing?.rotation ?? (
    state.selectedItemId === itemId ? state.selectedRotation : instance.rotations[0]
  );
  const indexedShape = rotatedShapeWithIndices(instance.shape, rotation);
  const grabbed = indexedShape.find(([row, col]) => row === offset[0] && col === offset[1])
    || indexedShape[0];
  state.pointerDrag = {
    itemId,
    pointerId: event.pointerId,
    source: event.currentTarget,
    originX: event.clientX,
    originY: event.clientY,
    lastX: event.clientX,
    lastY: event.clientY,
    rotation,
    active: false,
  };
  state.dragOffset = [grabbed[0], grabbed[1]];
  state.dragGrabIndex = grabbed[2];
  if (event.currentTarget.setPointerCapture) event.currentTarget.setPointerCapture(event.pointerId);
}

function activatePointerDrag() {
  const drag = state.pointerDrag;
  if (!drag || drag.active) return;
  const instance = instanceById(drag.itemId);
  if (!instance) return;
  const existing = state.placements.get(drag.itemId);
  state.draggingItemId = drag.itemId;
  state.selectedItemId = drag.itemId;
  state.selectedRotation = existing?.rotation ?? drag.rotation;
  drag.active = true;
  state.dragGhost = createDragGhost(instance, state.selectedRotation);
  document.body.classList.add("is-dragging-item");
  positionDragGhost(drag.lastX, drag.lastY);
  renderRotationControl();
}

function boardCellAt(clientX, clientY) {
  const target = document.elementFromPoint(clientX, clientY);
  const cell = target instanceof Element ? target.closest(".board-cell") : null;
  return cell && !cell.disabled ? cell : null;
}

function handlePointerMove(event) {
  const drag = state.pointerDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  drag.lastX = event.clientX;
  drag.lastY = event.clientY;
  if (!drag.active && Math.hypot(event.clientX - drag.originX, event.clientY - drag.originY) >= 5) {
    activatePointerDrag();
  }
  if (!drag.active) return;
  event.preventDefault();
  positionDragGhost(event.clientX, event.clientY);
  const cell = boardCellAt(event.clientX, event.clientY);
  if (cell) updateDragPreview(Number(cell.dataset.row), Number(cell.dataset.col));
  else clearDragPreview();
}

function clearDragPreview() {
  $("#board").classList.remove("is-drop-invalid");
  $$(
    ".board-cell.is-drop-valid, .board-cell.is-drop-invalid, "
      + ".board-cell.is-effect-range, .board-cell.is-effect-target",
  ).forEach((cell) => {
    cell.classList.remove(
      "is-drop-valid",
      "is-drop-invalid",
      "is-effect-range",
      "is-effect-target",
    );
  });
  $$(".effect-marker").forEach((marker) => marker.remove());
  state.dragAnchor = null;
  if (state.pointerDrag?.active) {
    $("#layout-message").className = "layout-message";
    $("#layout-message").textContent = "移到背包格子上查看占位和效果范围";
  }
}

function updateDragPreview(targetRow, targetCol) {
  const itemId = state.draggingItemId;
  const instance = instanceById(itemId);
  if (!itemId || !instance || !state.detail) return;
  const anchorRow = targetRow - state.dragOffset[0];
  const anchorCol = targetCol - state.dragOffset[1];
  state.dragAnchor = [anchorRow, anchorCol];
  const candidate = {
    item_id: itemId,
    row: anchorRow,
    col: anchorCol,
    rotation: state.selectedRotation,
  };
  const validCells = new Set(state.detail.valid_cells.map(([row, col]) => `${row},${col}`));
  const occupiedByOthers = new Set();
  for (const placement of state.placements.values()) {
    if (placement.item_id === itemId) continue;
    const other = instanceById(placement.item_id);
    if (!other) continue;
    occupiedCells(other, placement).forEach(([row, col]) => occupiedByOthers.add(`${row},${col}`));
  }
  const cells = occupiedCells(instance, candidate);
  const isValid = cells.every(([row, col]) => {
    const key = `${row},${col}`;
    return validCells.has(key) && !occupiedByOthers.has(key);
  });
  clearDragPreview();
  state.dragAnchor = [anchorRow, anchorCol];
  cells.forEach(([row, col]) => {
    const cell = document.querySelector(`.board-cell[data-row="${row}"][data-col="${col}"]`);
    if (cell) cell.classList.add(isValid ? "is-drop-valid" : "is-drop-invalid");
  });
  const effect = renderEffectPreview(instance, candidate);
  $("#board").classList.toggle("is-drop-invalid", !isValid);
  const message = $("#layout-message");
  message.className = isValid ? "layout-message" : "layout-message is-error";
  const effectSummary = effect.supportedEffects
    ? ` · 效果范围 ${effect.range.size} 格 · 当前命中 ${effect.targets.size} 件`
    : "";
  message.textContent = `${isValid ? "释放以放置" : "此处会产生越界或重叠"}${effectSummary} · 锚点 (${anchorRow},${anchorCol})`;
}

function finishPointerDrag(commit) {
  const drag = state.pointerDrag;
  if (!drag) return;
  const wasActive = drag.active;
  const dropped = wasActive && commit && state.draggingItemId && state.dragAnchor;
  if (dropped) {
    const [row, col] = state.dragAnchor;
    state.placements.set(state.draggingItemId, {
      item_id: state.draggingItemId,
      row,
      col,
      rotation: state.selectedRotation,
    });
  }
  if (drag.source?.hasPointerCapture?.(drag.pointerId)) {
    drag.source.releasePointerCapture(drag.pointerId);
  }
  if (wasActive) state.suppressClickUntil = Date.now() + 180;
  state.dragGhost?.remove();
  state.dragGhost = null;
  state.draggingItemId = null;
  state.dragOffset = [0, 0];
  state.dragGrabIndex = null;
  state.pointerDrag = null;
  document.body.classList.remove("is-dragging-item");
  clearDragPreview();
  if (wasActive) {
    renderLayout();
    if (dropped) evaluateLayout();
  }
}

function rotateActiveDrag() {
  const drag = state.pointerDrag;
  const itemId = state.draggingItemId;
  const instance = instanceById(itemId);
  if (!drag?.active || !itemId || !instance) return;
  const currentIndex = instance.rotations.indexOf(state.selectedRotation);
  state.selectedRotation = instance.rotations[(currentIndex + 1) % instance.rotations.length];
  const grabbed = rotatedShapeWithIndices(instance.shape, state.selectedRotation)
    .find(([, , index]) => index === state.dragGrabIndex);
  if (grabbed) state.dragOffset = [grabbed[0], grabbed[1]];
  state.dragGhost?.remove();
  state.dragGhost = createDragGhost(instance, state.selectedRotation);
  positionDragGhost(drag.lastX, drag.lastY);
  renderRotationControl();
  const cell = boardCellAt(drag.lastX, drag.lastY);
  if (cell) updateDragPreview(Number(cell.dataset.row), Number(cell.dataset.col));
  else clearDragPreview();
}

function cycleRotation(itemId) {
  if (state.pointerDrag?.active && state.draggingItemId === itemId) {
    rotateActiveDrag();
    return;
  }
  const instance = instanceById(itemId);
  if (!instance) return;
  const existing = state.placements.get(itemId);
  const current = existing?.rotation ?? (
    state.selectedItemId === itemId ? state.selectedRotation : instance.rotations[0]
  );
  const index = instance.rotations.indexOf(current);
  const next = instance.rotations[(index + 1) % instance.rotations.length];
  state.selectedItemId = itemId;
  state.selectedRotation = next;
  if (existing) {
    existing.rotation = next;
    state.placements.set(itemId, existing);
  }
  renderLayout();
  evaluateLayout();
}

function analyzeLayout() {
  const valid = new Set((state.detail?.valid_cells || []).map(([row, col]) => `${row},${col}`));
  const owners = new Map();
  const invalidItems = new Set();
  const conflicts = new Set();
  for (const placement of state.placements.values()) {
    const instance = instanceById(placement.item_id);
    if (!instance) {
      invalidItems.add(placement.item_id);
      continue;
    }
    for (const [row, col] of occupiedCells(instance, placement)) {
      const key = `${row},${col}`;
      if (!valid.has(key)) invalidItems.add(placement.item_id);
      if (!owners.has(key)) owners.set(key, []);
      owners.get(key).push(placement.item_id);
      if (owners.get(key).length > 1) {
        conflicts.add(key);
        owners.get(key).forEach((id) => invalidItems.add(id));
      }
    }
  }
  return { owners, invalidItems, conflicts };
}

function renderScenarioStrip() {
  const summary = currentScenarioSummary();
  const strip = $("#scenario-strip");
  strip.replaceChildren();
  if (!summary) return;
  const title = document.createElement("strong");
  title.textContent = summary.title;
  strip.append(title);
  [summary.difficulty, ...summary.tags].forEach((value) => {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = value;
    strip.append(tag);
  });
  const dimensions = document.createElement("span");
  dimensions.textContent = `${summary.board.height}×${summary.board.width} · ${summary.instances} 件物品`;
  strip.append(dimensions);
  const hash = document.createElement("span");
  hash.className = "hash-text";
  hash.textContent = `scenario ${summary.scenario_hash.slice(0, 10)}…`;
  strip.append(hash);
}

function renderVisualScenarioInput() {
  if (!state.detail) return;
  const mode = $("#visual-sheet-mode").value;
  $("#visual-sheet").src = state.detail.sheet_urls[mode];
  $("#visual-prompt-text").textContent = state.detail.visual_prompts[mode];
}

function renderInventory() {
  const container = $("#inventory");
  container.replaceChildren();
  if (!state.detail) return;
  state.detail.instances.forEach((instance) => {
    const row = document.createElement("div");
    row.className = "inventory-row";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "item-button";
    if (state.selectedItemId === instance.item_id) button.classList.add("is-selected");
    if (state.placements.has(instance.item_id)) button.classList.add("is-placed");
    const image = document.createElement("img");
    image.className = "item-image";
    image.src = instance.image_url;
    image.alt = "";
    image.draggable = false;
    const copy = document.createElement("span");
    copy.className = "item-copy";
    const name = document.createElement("span");
    name.className = "item-name";
    name.textContent = `${instance.display_name} · ${instance.item_id}`;
    const stat = document.createElement("span");
    stat.className = "item-stat";
    const stats = Object.entries(instance.stats_zh || instance.stats)
      .map(([name, value]) => `${STAT_LABELS[name] || name} ${value}`)
      .join(" · ");
    const category = instance.category_label || CATEGORY_LABELS[instance.category]
      || instance.category;
    stat.textContent = [category, stats].filter(Boolean).join(" · ");
    copy.append(name, stat);
    button.append(image, copy);
    button.addEventListener("click", () => selectItem(instance.item_id));
    button.addEventListener("pointerdown", (event) => {
      armPointerDrag(instance.item_id, [0, 0], event);
    });
    button.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      cycleRotation(instance.item_id);
    });
    row.append(button);
    if (state.placements.has(instance.item_id)) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove-item";
      remove.setAttribute("aria-label", `移除 ${instance.item_id}`);
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        state.placements.delete(instance.item_id);
        renderLayout();
        evaluateLayout();
      });
      row.append(remove);
    }
    container.append(row);
  });
  $("#placed-count").textContent = `${state.placements.size} / ${state.detail.instances.length}`;
}

function selectItem(itemId) {
  state.selectedItemId = itemId;
  const instance = instanceById(itemId);
  const existing = state.placements.get(itemId);
  state.selectedRotation = existing?.rotation ?? instance?.rotations[0] ?? 0;
  renderInventory();
  renderRotationControl();
}

function renderRotationControl() {
  const select = $("#rotation-select");
  const footprint = $("#footprint-text");
  const detail = $("#selected-item-detail");
  select.replaceChildren();
  detail.replaceChildren();
  const instance = instanceById(state.selectedItemId);
  if (!instance) {
    select.disabled = true;
    footprint.textContent = "请选择一个物品";
    return;
  }
  select.disabled = false;
  instance.rotations.forEach((rotation) => {
    const option = document.createElement("option");
    option.value = String(rotation);
    option.textContent = `${rotation}°`;
    option.selected = rotation === state.selectedRotation;
    select.append(option);
  });
  const shape = rotatedShape(instance.shape, state.selectedRotation);
  const height = Math.max(...shape.map(([row]) => row)) + 1;
  const width = Math.max(...shape.map(([, col]) => col)) + 1;
  footprint.textContent = `${instance.display_name} · ${height}×${width} · ${shape.length} 格`;
  const base = document.createElement("span");
  const stats = Object.entries(instance.stats_zh || instance.stats)
    .map(([name, value]) => `${STAT_LABELS[name] || name}=${value}`)
    .join("，");
  const category = instance.category_label || CATEGORY_LABELS[instance.category]
    || instance.category;
  base.textContent = `类别 ${category} · ${stats || "无基础属性"}`;
  detail.append(base);
  (instance.effect_descriptions || []).forEach((description) => {
    const line = document.createElement("span");
    line.className = "effect-description";
    line.textContent = description;
    detail.append(line);
  });
}

function renderBoard() {
  const board = $("#board");
  board.replaceChildren();
  if (!state.detail) return;
  const scenario = state.detail.scenario;
  board.style.setProperty("--cols", scenario.board.width);
  const valid = new Set(state.detail.valid_cells.map(([row, col]) => `${row},${col}`));
  const analysis = analyzeLayout();
  for (let row = 0; row < scenario.board.height; row += 1) {
    for (let col = 0; col < scenario.board.width; col += 1) {
      const key = `${row},${col}`;
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "board-cell";
      cell.dataset.row = String(row);
      cell.dataset.col = String(col);
      cell.setAttribute("role", "gridcell");
      cell.setAttribute("aria-label", `格子 (${row},${col})`);
      const coordinate = document.createElement("span");
      coordinate.className = "cell-coordinate";
      coordinate.textContent = `${row},${col}`;
      cell.append(coordinate);
      if (!valid.has(key)) {
        cell.classList.add("is-hole");
        cell.disabled = true;
        const mark = document.createElement("span");
        mark.textContent = "×";
        cell.append(mark);
      } else {
        const owners = analysis.owners.get(key) || [];
        const owner = instanceById(owners[0]);
        if (owner) {
          cell.classList.add("is-draggable");
          cell.classList.add(owner.category === "weapon" ? "is-weapon" : "is-support");
          const item = document.createElement("span");
          item.className = "cell-item";
          item.textContent = owner.display_name.slice(0, 3);
          const suffix = document.createElement("span");
          suffix.className = "cell-instance";
          suffix.textContent = `#${owner.item_id.split("_").at(-1)}`;
          cell.append(item, suffix);
          cell.addEventListener("pointerdown", (event) => {
            const placement = state.placements.get(owner.item_id);
            const offset = placement ? [row - placement.row, col - placement.col] : [0, 0];
            armPointerDrag(owner.item_id, offset, event);
          });
        }
        if (analysis.conflicts.has(key)) cell.classList.add("is-conflict");
        cell.addEventListener("click", () => {
          if (Date.now() >= state.suppressClickUntil) placeSelected(row, col);
        });
        cell.addEventListener("contextmenu", (event) => {
          event.preventDefault();
          const itemId = owner?.item_id || state.selectedItemId;
          if (itemId) cycleRotation(itemId);
        });
      }
      board.append(cell);
    }
  }
  const message = $("#layout-message");
  if (analysis.invalidItems.size) {
    message.className = "layout-message is-error";
    message.textContent = `本地预检：${Array.from(analysis.invalidItems).join("、")} 存在越界或重叠`;
  } else if (state.selectedItemId) {
    message.className = "layout-message";
    message.textContent = `当前选择 ${state.selectedItemId}，点击任意合法格作为旋转后外接矩形左上角`;
  } else {
    message.className = "layout-message";
    message.textContent = "从左侧选择一个物品开始摆放";
  }
}

function placeSelected(row, col) {
  if (!state.selectedItemId) return;
  state.placements.set(state.selectedItemId, {
    item_id: state.selectedItemId,
    row,
    col,
    rotation: state.selectedRotation,
  });
  renderLayout();
  evaluateLayout();
}

function renderPlacementList() {
  const list = $("#placement-list");
  list.replaceChildren();
  const placements = Array.from(state.placements.values()).sort((a, b) =>
    a.item_id.localeCompare(b.item_id),
  );
  if (!placements.length) {
    const empty = document.createElement("div");
    empty.className = "empty-note";
    empty.textContent = "暂无物品";
    list.append(empty);
    return;
  }
  placements.forEach((placement) => {
    const item = document.createElement("div");
    item.className = "placement-entry";
    const name = document.createElement("strong");
    name.textContent = instanceById(placement.item_id)?.display_name || placement.item_id;
    const coordinates = document.createElement("span");
    coordinates.textContent = `(${placement.row},${placement.col}) · ${placement.rotation}°`;
    item.append(name, coordinates);
    list.append(item);
  });
}

function renderLayout() {
  renderInventory();
  renderRotationControl();
  renderBoard();
  renderPlacementList();
}

async function evaluateLayout() {
  if (!state.detail || !state.suiteId || !state.scenarioId) return;
  const version = ++state.evaluationVersion;
  try {
    const result = await api("/api/evaluate", {
      method: "POST",
      body: JSON.stringify({
        suite_id: state.suiteId,
        scenario_id: state.scenarioId,
        placements: Array.from(state.placements.values()),
      }),
    });
    if (version !== state.evaluationVersion) return;
    renderScore(result);
  } catch (error) {
    if (version !== state.evaluationVersion) return;
    renderScore({ valid: false, actual_attack: 0, errors: [{ message: error.message }] });
  }
}

function renderScore(result) {
  const oracle = Number(state.detail?.oracle.optimal_attack || 0);
  const actual = Number(result.actual_attack || 0);
  const ratio = oracle ? Math.min(1, actual / oracle) : 0;
  $("#actual-score").textContent = String(actual);
  $("#oracle-score").textContent = oracle ? String(oracle) : "—";
  $("#ratio-fill").style.width = `${ratio * 100}%`;
  const statusNode = $("#score-status");
  if (result.valid) {
    statusNode.className = "score-status is-valid";
    statusNode.textContent = actual === oracle ? "已命中精确最优" : "合法布局";
  } else {
    statusNode.className = "score-status is-error";
    statusNode.textContent = "布局不合法 · 本次得分为 0";
  }
  const meta = $("#score-meta");
  if (result.valid) {
    meta.textContent = `${result.placements} 件 · ${result.used_cells} 格 · ${result.effects?.length || 0} 次效果结算 · ${(ratio * 100).toFixed(1)}% Oracle`;
  } else {
    const messages = (result.errors || []).slice(0, 3).map((error) =>
      error.code || error.message || "unknown error",
    );
    meta.textContent = messages.join(" · ") || "请检查摆放";
  }
}

async function loadScenario() {
  if (!state.suiteId || !state.scenarioId) return;
  state.detail = null;
  state.placements.clear();
  state.selectedItemId = null;
  $("#board").replaceChildren();
  try {
    state.detail = await api(
      `/api/suites/${encodeURIComponent(state.suiteId)}/scenarios/${encodeURIComponent(state.scenarioId)}`,
    );
    const first = state.detail.instances[0];
    state.selectedItemId = first?.item_id || null;
    state.selectedRotation = first?.rotations[0] || 0;
    renderScenarioStrip();
    renderLayout();
    $("#prompt-text").textContent = state.detail.prompt;
    $("#prompt-meta").textContent = `prompt ${state.detail.prompt_hash} · scenario ${state.detail.scenario_hash}`;
    renderVisualScenarioInput();
    await evaluateLayout();
  } catch (error) {
    $("#layout-message").className = "layout-message is-error";
    $("#layout-message").textContent = error.message;
  }
}

function populateScenarioSelect() {
  const suite = currentSuite();
  const select = $("#scenario-select");
  select.replaceChildren();
  (suite?.scenarios || []).forEach((scenario) => {
    const option = document.createElement("option");
    option.value = scenario.id;
    option.textContent = `${scenario.title} · Oracle ${scenario.oracle_attack}`;
    select.append(option);
  });
  state.scenarioId = suite?.scenarios[0]?.id || null;
  select.value = state.scenarioId || "";
  loadScenario();
}

async function loadSuites() {
  try {
    const [health, suites] = await Promise.all([api("/api/health"), api("/api/suites")]);
    state.suites = suites;
    setServerState("online", `${health.suites} 个题集 · ${health.run_configs} 个运行配置`);
    const select = $("#suite-select");
    select.replaceChildren();
    suites.forEach((suite) => {
      const option = document.createElement("option");
      option.value = suite.id;
      option.textContent = `${suite.title} (${suite.scenarios.length})`;
      select.append(option);
    });
    state.suiteId = suites[0]?.id || null;
    select.value = state.suiteId || "";
    populateScenarioSelect();
  } catch (error) {
    setServerState("error", error.message);
  }
}

function loadOracle() {
  const witness = state.detail?.oracle.witness?.placements || [];
  state.placements = new Map(witness.map((placement) => [placement.item_id, { ...placement }]));
  if (witness.length) selectItem(witness[0].item_id);
  renderLayout();
  evaluateLayout();
}

function clearLayout() {
  state.placements.clear();
  renderLayout();
  evaluateLayout();
}

function currentRunConfig() {
  return state.runConfigs.find((config) => config.id === state.runConfigId) || null;
}

function usingBrowserProfile() {
  return $("#model-source-select").value === "browser";
}

function optionalNumber(selector, integer = false) {
  const raw = $(selector).value.trim();
  if (!raw) return undefined;
  const value = Number(raw);
  if (!Number.isFinite(value)) return undefined;
  return integer ? Math.trunc(value) : value;
}

function collectApiProfile() {
  const protocol = $("#api-protocol").value;
  const params = { json_mode: $("#api-json-mode").checked };
  const temperature = optionalNumber("#api-temperature");
  const maxTokens = optionalNumber("#api-max-tokens", true);
  const thinkingEffort = $("#api-thinking-effort").value;
  if (temperature !== undefined) params.temperature = temperature;
  if (maxTokens !== undefined) params.max_tokens = maxTokens;
  if (thinkingEffort) params.thinking_effort = thinkingEffort;
  if (protocol === "anthropic_messages") {
    const thinkingMode = $("#api-thinking-mode").value;
    const thinkingBudget = optionalNumber("#api-thinking-budget", true);
    if (thinkingMode) params.thinking_mode = thinkingMode;
    if (thinkingBudget !== undefined) params.thinking_budget = thinkingBudget;
  }
  const limits = {
    concurrency: optionalNumber("#api-concurrency", true) ?? 1,
    timeout_seconds: optionalNumber("#api-timeout") ?? 120,
    retries: optionalNumber("#api-retries", true) ?? 3,
  };
  const qps = optionalNumber("#api-qps");
  if (qps !== undefined) limits.qps = qps;
  const profile = {
    display_name: $("#api-profile-name").value.trim() || undefined,
    protocol,
    base_url: $("#api-base-url").value.trim(),
    endpoint: $("#api-endpoint").value.trim() || undefined,
    model: $("#api-model").value.trim(),
    api_key: $("#api-key").value,
    params,
    limits,
    verify_tls: $("#api-verify-tls").checked,
  };
  const authMode = $("#api-auth-mode").value;
  if (authMode) profile.auth_mode = authMode;
  return profile;
}

function browserProfileReady() {
  const profile = collectApiProfile();
  if (!profile.base_url || !profile.model) return false;
  try {
    const url = new URL(profile.base_url);
    if (!["http:", "https:"].includes(url.protocol)) return false;
  } catch {
    return false;
  }
  const authMode = profile.auth_mode || (profile.protocol === "openai_chat" ? "bearer" : "x-api-key");
  if (authMode !== "none" && !profile.api_key) return false;
  return Array.from(document.querySelectorAll("#api-profile-fields input"))
    .every((input) => (
      profile.protocol === "openai_chat" && input.id === "api-thinking-budget"
        ? true
        : input.checkValidity()
    ));
}

function runSourceReady() {
  if (!state.runPreview) return false;
  return usingBrowserProfile() ? browserProfileReady() : state.runPreview.key_ready;
}

function setApiSaveState(message, kind = "") {
  const node = $("#api-save-state");
  node.textContent = message;
  node.className = kind ? `is-${kind}` : "";
}

function apiHistoryLabel(record) {
  const profile = record.profile;
  let host = profile.base_url;
  try {
    host = new URL(profile.base_url).host;
  } catch {
    // Keep the original URL for a legacy or incomplete record.
  }
  const protocol = profile.protocol === "anthropic_messages" ? "Anthropic" : "OpenAI";
  return `${profile.display_name || profile.model} · ${host} · ${protocol}`;
}

function renderApiHistorySelect() {
  const select = $("#api-history-select");
  select.replaceChildren();
  const fresh = document.createElement("option");
  fresh.value = "";
  fresh.textContent = "新 API 配置";
  select.append(fresh);
  state.apiHistory.forEach((record) => {
    const option = document.createElement("option");
    option.value = record.id;
    option.textContent = apiHistoryLabel(record);
    select.append(option);
  });
  select.value = state.apiHistoryId || "";
  $("#delete-api-profile").disabled = !state.apiHistoryId;
}

function resetApiForm() {
  $("#api-profile-name").value = "";
  $("#api-protocol").value = "openai_chat";
  $("#api-base-url").value = "";
  $("#api-model").value = "";
  $("#api-key").value = "";
  $("#api-key").type = "password";
  $("#toggle-api-key").textContent = "显示";
  $("#toggle-api-key").setAttribute("aria-label", "显示 API Key");
  $("#api-auth-mode").value = "";
  $("#api-endpoint").value = "";
  $("#api-thinking-effort").value = "";
  $("#api-thinking-mode").value = "";
  $("#api-thinking-budget").value = "";
  $("#api-max-tokens").value = "";
  $("#api-temperature").value = "";
  $("#api-timeout").value = "120";
  $("#api-concurrency").value = "1";
  $("#api-qps").value = "1";
  $("#api-retries").value = "3";
  $("#api-verify-tls").checked = true;
  $("#api-json-mode").checked = false;
  $("#remember-api-key").checked = true;
  syncProtocolFields();
}

function applyApiHistoryRecord(record) {
  const profile = record.profile;
  const params = profile.params || {};
  const limits = profile.limits || {};
  $("#api-profile-name").value = profile.display_name || "";
  $("#api-protocol").value = profile.protocol || "openai_chat";
  $("#api-base-url").value = profile.base_url || "";
  $("#api-model").value = profile.model || "";
  $("#api-key").value = profile.api_key || "";
  $("#api-key").type = "password";
  $("#toggle-api-key").textContent = "显示";
  $("#toggle-api-key").setAttribute("aria-label", "显示 API Key");
  $("#api-auth-mode").value = profile.auth_mode || "";
  $("#api-endpoint").value = profile.endpoint || "";
  $("#api-thinking-effort").value = params.thinking_effort || "";
  $("#api-thinking-mode").value = params.thinking_mode || "";
  $("#api-thinking-budget").value = params.thinking_budget || "";
  $("#api-max-tokens").value = params.max_tokens || "";
  $("#api-temperature").value = params.temperature ?? "";
  $("#api-timeout").value = limits.timeout_seconds ?? 120;
  $("#api-concurrency").value = limits.concurrency ?? 1;
  $("#api-qps").value = limits.qps ?? "";
  $("#api-retries").value = limits.retries ?? 3;
  $("#api-verify-tls").checked = profile.verify_tls !== false;
  $("#api-json-mode").checked = params.json_mode === true;
  $("#remember-api-key").checked = record.remember_key !== false;
  syncProtocolFields();
}

function persistApiHistory() {
  try {
    localStorage.setItem(API_HISTORY_STORAGE_KEY, JSON.stringify(state.apiHistory));
    return true;
  } catch {
    setApiSaveState("浏览器存储不可用", "error");
    return false;
  }
}

function makeHistoryId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function saveCurrentApiHistory() {
  const profile = collectApiProfile();
  if (!profile.base_url || !profile.model) {
    setApiSaveState("填写 URL 和模型名后自动保存");
    return null;
  }
  const rememberKey = $("#remember-api-key").checked;
  const storedProfile = JSON.parse(JSON.stringify(profile));
  if (!rememberKey) storedProfile.api_key = "";
  const id = state.apiHistoryId || makeHistoryId();
  const record = {
    id,
    profile: storedProfile,
    remember_key: rememberKey,
    updated_at: new Date().toISOString(),
  };
  state.apiHistory = [record, ...state.apiHistory.filter((item) => item.id !== id)]
    .slice(0, 50);
  state.apiHistoryId = id;
  if (!persistApiHistory()) return null;
  renderApiHistorySelect();
  setApiSaveState(
    rememberKey && storedProfile.api_key
      ? "已自动保存（含 Key）"
      : "已自动保存（不含 Key）",
    "saved",
  );
  return profile;
}

function scheduleApiHistorySave() {
  if (state.apiSaveTimer) clearTimeout(state.apiSaveTimer);
  if (!usingBrowserProfile()) return;
  setApiSaveState("等待自动保存…");
  state.apiSaveTimer = setTimeout(() => {
    state.apiSaveTimer = null;
    saveCurrentApiHistory();
  }, 600);
  renderRunPreview();
}

function flushApiHistorySave(force = false) {
  const pending = state.apiSaveTimer !== null;
  if (pending) clearTimeout(state.apiSaveTimer);
  state.apiSaveTimer = null;
  return pending || force ? saveCurrentApiHistory() : null;
}

function loadApiHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(API_HISTORY_STORAGE_KEY) || "[]");
    state.apiHistory = Array.isArray(parsed)
      ? parsed.filter((record) => record && typeof record.id === "string" && record.profile)
        .slice(0, 50)
      : [];
  } catch {
    state.apiHistory = [];
  }
  state.apiHistoryId = state.apiHistory[0]?.id || null;
  if (state.apiHistory[0]) applyApiHistoryRecord(state.apiHistory[0]);
  else resetApiForm();
  renderApiHistorySelect();
  setApiSaveState(state.apiHistory.length ? "已载入最近使用的 API" : "尚未保存");
}

function selectApiHistory(recordId) {
  flushApiHistorySave();
  state.apiHistoryId = recordId || null;
  const record = state.apiHistory.find((item) => item.id === recordId);
  if (record) {
    applyApiHistoryRecord(record);
    setApiSaveState("已载入历史 API", "saved");
  } else {
    resetApiForm();
    setApiSaveState("新配置将在填写后自动保存");
  }
  renderApiHistorySelect();
  renderRunPreview();
}

function newApiHistory() {
  selectApiHistory("");
  $("#api-profile-name").focus();
}

function deleteApiHistory() {
  flushApiHistorySave();
  if (!state.apiHistoryId) return;
  state.apiHistory = state.apiHistory.filter((item) => item.id !== state.apiHistoryId);
  persistApiHistory();
  state.apiHistoryId = state.apiHistory[0]?.id || null;
  if (state.apiHistory[0]) applyApiHistoryRecord(state.apiHistory[0]);
  else resetApiForm();
  renderApiHistorySelect();
  setApiSaveState(state.apiHistory.length ? "已删除，载入上一条记录" : "历史记录已清空");
  renderRunPreview();
}

function syncProtocolFields() {
  const anthropic = $("#api-protocol").value === "anthropic_messages";
  $("#anthropic-thinking-mode-field").hidden = !anthropic;
  $("#anthropic-thinking-budget-field").hidden = !anthropic;
}

function syncModelSource() {
  const browser = usingBrowserProfile();
  $("#api-profile-fields").disabled = !browser;
  $("#api-profile-panel").classList.toggle("is-disabled", !browser);
  renderRunPreview();
}

function currentRunRequest() {
  return usingBrowserProfile() ? { profile: collectApiProfile() } : {};
}

function setRunNotice(message, isError = false) {
  const notice = $("#run-notice");
  notice.textContent = message;
  notice.classList.toggle("is-error", isError);
}

function renderRunPreview() {
  const preview = state.runPreview;
  const container = $("#run-preview");
  container.replaceChildren();
  if (!preview) return;
  const browser = usingBrowserProfile();
  const ready = runSourceReady();
  const profile = browser ? collectApiProfile() : null;
  const modelConcurrency = browser ? Number(profile.limits.concurrency || 1) : null;
  const concurrencyCap = browser
    ? Math.min(Number(preview.concurrency), modelConcurrency)
    : Number(preview.concurrency);
  const values = [
    ["题集", preview.suite_id],
    ["题面", preview.prompt_mode === "text" ? "纯文字" : preview.prompt_mode],
    ["Jobs", String(browser ? preview.scenarios * preview.trials : preview.jobs)],
    ["模型配置", String(browser ? 1 : preview.profiles.length)],
    [browser ? "实际并发上限" : "全局并发", String(concurrencyCap)],
    [
      "鉴权",
      browser && profile.auth_mode === "none"
        ? "无需 Key"
        : ready ? "Key 已就绪" : "Key 缺失",
    ],
  ];
  values.forEach(([label, value], index) => {
    const card = document.createElement("div");
    card.className = "preview-card";
    const labelNode = document.createElement("span");
    labelNode.textContent = label;
    const valueNode = document.createElement("strong");
    valueNode.textContent = value;
    if (index === 4) valueNode.className = ready ? "is-ready" : "is-missing";
    card.append(labelNode, valueNode);
    container.append(card);
  });
  $("#start-run").disabled = !ready;
  if (browser) {
    const model = profile.model || "尚未填写模型";
    const endpoint = profile.base_url || "尚未填写 API URL";
    const qps = profile.limits.qps;
    const limitNotes = [`实际并发上限 ${concurrencyCap}`];
    if (modelConcurrency > preview.concurrency) {
      limitNotes.push(`受 run.yaml 全局并发 ${preview.concurrency} 限制`);
    }
    if (qps != null) {
      const interval = Math.round((1 / qps) * 100) / 100;
      limitNotes.push(`QPS ${qps}（约每 ${interval} 秒启动一个请求）`);
    }
    setRunNotice(
      ready
        ? `前端 API：${model} · ${endpoint} · ${limitNotes.join(" · ")}`
        : "请填写有效的 API URL、模型名和 API Key",
      !ready,
    );
  } else {
    const models = preview.profiles.map((item) => `${item.id} / ${item.model}`).join("；");
    setRunNotice(
      ready ? models : "API key 尚未就绪：请填写项目根目录的 .env 后重启 Web 服务",
      !ready,
    );
  }
}

async function loadRunConfig() {
  if (!state.runConfigId) return;
  state.selectedRunId = null;
  stopPolling();
  stopRunStream();
  try {
    state.runPreview = await api(
      `/api/run-configs/${encodeURIComponent(state.runConfigId)}/preview`,
    );
    renderRunPreview();
    await refreshRuns();
  } catch (error) {
    setRunNotice(error.message, true);
  }
}

async function loadRunConfigs() {
  try {
    state.runConfigs = await api("/api/run-configs");
    const select = $("#run-config-select");
    select.replaceChildren();
    state.runConfigs.forEach((config) => {
      const option = document.createElement("option");
      option.value = config.id;
      const mode = config.prompt_mode === "text" ? "文字" : "视觉";
      option.textContent = `${config.plan_id} · ${mode} · ${config.path}`;
      select.append(option);
    });
    state.runConfigId = state.runConfigs[0]?.id || null;
    select.value = state.runConfigId || "";
    if (state.runConfigId) {
      await loadRunConfig();
    } else {
      setRunNotice("没有发现可用的 run.yaml；请在 configs/ 下添加配置", true);
      $("#start-run").disabled = true;
    }
  } catch (error) {
    setRunNotice(error.message, true);
  }
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function statusBadge(status) {
  const span = document.createElement("span");
  span.className = "status-badge";
  span.dataset.status = status;
  span.textContent = RUN_STATUS_LABELS[status] || status;
  return span;
}

function runIsActive(status) {
  return ["running", "starting", "stopping"].includes(status);
}

function renderRunsTable(runs) {
  const tbody = $("#runs-table");
  tbody.replaceChildren();
  if (!runs.length) {
    tbody.append($("#empty-row-template").content.cloneNode(true));
    return;
  }
  runs.forEach((run) => {
    const row = document.createElement("tr");
    row.dataset.runId = run.run_id;
    row.classList.toggle("is-selected", run.run_id === state.selectedRunId);
    const id = document.createElement("td");
    id.className = "run-id-cell";
    id.textContent = run.run_id;
    const model = document.createElement("td");
    model.textContent = (run.profiles || []).map((profile) => profile.model).join("、") || "—";
    const status = document.createElement("td");
    status.className = "run-status-cell";
    status.append(statusBadge(run.status));
    const progress = document.createElement("td");
    progress.className = "run-progress-cell";
    progress.textContent = `${run.progress.completed}/${run.progress.total}`;
    const started = document.createElement("td");
    started.textContent = formatDate(run.started_at);
    row.append(id, model, status, progress, started);
    row.addEventListener("click", () => selectRun(run.run_id));
    tbody.append(row);
  });
}

async function refreshRuns() {
  if (!state.runConfigId) return;
  try {
    const runs = await api(`/api/run-configs/${encodeURIComponent(state.runConfigId)}/runs`);
    renderRunsTable(runs);
    if (state.selectedRunId) await loadRunStatus(state.selectedRunId);
  } catch (error) {
    setRunNotice(error.message, true);
  }
}

async function startRun() {
  if (!state.runConfigId) return;
  if (!runSourceReady()) {
    renderRunPreview();
    return;
  }
  if (usingBrowserProfile()) flushApiHistorySave(true);
  $("#start-run").disabled = true;
  setRunNotice("正在创建运行任务…");
  try {
    const result = await api(`/api/run-configs/${encodeURIComponent(state.runConfigId)}/runs`, {
      method: "POST",
      body: JSON.stringify(currentRunRequest()),
    });
    state.selectedRunId = result.run_id;
    setRunNotice(`Run ${result.run_id} 已启动`);
    await refreshRuns();
    await loadRunStatus(result.run_id);
  } catch (error) {
    setRunNotice(error.message, true);
    $("#start-run").disabled = !runSourceReady();
  }
}

function stopPolling() {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function setRunStreamState(text, streamState = "") {
  const node = $("#run-stream-state");
  node.textContent = text;
  if (streamState) node.dataset.state = streamState;
  else delete node.dataset.state;
}

function stopRunStream(label = "未连接", streamState = "") {
  if (state.runEventSource) state.runEventSource.close();
  state.runEventSource = null;
  state.runEventRunId = null;
  setRunStreamState(label, streamState);
}

function schedulePolling(runId) {
  stopPolling();
  state.pollTimer = setTimeout(async () => {
    if (state.selectedRunId !== runId) return;
    await refreshRuns();
  }, 1200);
}

function selectRun(runId) {
  stopPolling();
  stopRunStream("正在连接…", "connecting");
  state.selectedRunId = runId;
  loadRunStatus(runId);
}

function renderReportProfiles(report) {
  const tbody = $("#score-table");
  tbody.replaceChildren();
  (report?.profiles || []).forEach((profile) => {
    const row = document.createElement("tr");
    [
      profile.display_name || profile.profile_id,
      Number(profile.overall_score).toFixed(2),
      Number(profile.best_of_3_score).toFixed(2),
      `${(profile.valid_rate * 100).toFixed(1)}%`,
      `${(profile.optimal_hit_rate * 100).toFixed(1)}%`,
      profile.latency_p50_ms == null ? "—" : `${profile.latency_p50_ms.toFixed(0)} ms`,
    ].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    });
    tbody.append(row);
  });
}

function renderRunJobs(jobs) {
  const tbody = $("#run-jobs-table");
  tbody.replaceChildren();
  if (!jobs.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "empty-cell";
    cell.textContent = "任务尚未展开";
    row.append(cell);
    tbody.append(row);
    return;
  }
  jobs.forEach((job) => {
    const row = document.createElement("tr");
    row.dataset.status = job.status;
    const status = document.createElement("td");
    status.append(statusBadge(job.status));
    if (job.error_type) status.title = job.error_type;
    const scenario = document.createElement("td");
    scenario.textContent = job.title || job.scenario_id;
    scenario.title = job.scenario_id;
    const trial = document.createElement("td");
    trial.textContent = String(job.trial);
    const score = document.createElement("td");
    score.textContent = job.actual_attack == null
      ? "—"
      : `${job.actual_attack} / ${job.oracle_attack}`;
    score.title = "实际攻击 / Oracle";
    const latency = document.createElement("td");
    if (job.latency_ms == null) {
      latency.textContent = job.status === "running" ? "生成中…" : "—";
    } else if (job.latency_ms < 1000) {
      latency.textContent = `${Number(job.latency_ms).toFixed(0)} ms`;
    } else {
      latency.textContent = `${(Number(job.latency_ms) / 1000).toFixed(2)} s`;
    }
    const tokens = document.createElement("td");
    tokens.textContent = job.output_tokens == null
      ? "—"
      : `${job.output_tokens_estimated ? "≈" : ""}${Number(job.output_tokens).toLocaleString("zh-CN")}`;
    row.append(status, scenario, trial, score, latency, tokens);
    tbody.append(row);
  });
}

function updateRunListRow(payload) {
  const row = Array.from($("#runs-table").querySelectorAll("tr[data-run-id]")).find(
    (item) => item.dataset.runId === payload.run_id,
  );
  if (!row) return;
  const status = row.querySelector(".run-status-cell");
  const progress = row.querySelector(".run-progress-cell");
  if (status) status.replaceChildren(statusBadge(payload.status));
  if (progress) progress.textContent = `${payload.progress.completed}/${payload.progress.total}`;
}

function renderRunProgress(payload) {
  $("#run-detail-empty").hidden = true;
  $("#run-detail").hidden = false;
  $("#selected-run-id").textContent = payload.run_id;
  const badge = $("#run-status-badge");
  badge.textContent = RUN_STATUS_LABELS[payload.status] || payload.status;
  badge.dataset.status = payload.status;
  const progress = payload.progress;
  const percent = progress.total ? (progress.completed / progress.total) * 100 : 0;
  $("#run-progress-label").textContent = `${progress.completed} / ${progress.total} jobs`;
  $("#run-progress-fill").style.width = `${percent}%`;
  const metrics = $("#run-metrics");
  metrics.replaceChildren();
  const outputTokens = (payload.jobs || []).reduce(
    (total, job) => total + Number(job.output_tokens || 0),
    0,
  );
  const hasEstimatedTokens = (payload.jobs || []).some(
    (job) => job.output_tokens != null && job.output_tokens_estimated,
  );
  [
    ["尝试次数", progress.attempts],
    ["合法结果", progress.valid],
    ["运行中", progress.running],
    ["输出 Token", `${hasEstimatedTokens ? "≈" : ""}${outputTokens.toLocaleString("zh-CN")}`],
  ].forEach(([label, value]) => {
    const item = document.createElement("div");
    item.className = "metric";
    const labelNode = document.createElement("span");
    labelNode.textContent = label;
    const valueNode = document.createElement("strong");
    valueNode.textContent = String(value);
    item.append(labelNode, valueNode);
    metrics.append(item);
  });
  const error = $("#run-error");
  error.hidden = !payload.error;
  error.textContent = payload.error || "";
  const canResume = ["failed", "interrupted"].includes(payload.status);
  $("#resume-run").hidden = !canResume;
  const canStop = ["running", "starting", "stopping"].includes(payload.status);
  const stopButton = $("#stop-run");
  stopButton.hidden = !canStop;
  stopButton.disabled = payload.status === "stopping";
  stopButton.textContent = payload.status === "stopping" ? "中断中…" : "中断";
  $("#delete-run").hidden = canStop;
  renderRunJobs(payload.jobs || []);
  updateRunListRow(payload);
  $("#start-run").disabled = runIsActive(payload.status) || !runSourceReady();
}

function renderRunDetail(payload) {
  renderRunProgress(payload);
  renderReportProfiles(payload.report);
  const actions = $("#report-actions");
  actions.replaceChildren();
  if (payload.report) {
    [["JSON", "json"], ["CSV", "csv"], ["HTML", "html"]].forEach(([label, format]) => {
      const link = document.createElement("a");
      link.href = `/api/run-configs/${encodeURIComponent(state.runConfigId)}/runs/${encodeURIComponent(payload.run_id)}/report?format=${format}`;
      link.target = format === "csv" ? "_self" : "_blank";
      link.rel = "noopener";
      link.textContent = `${label} 报告`;
      actions.append(link);
    });
  }
}

function startRunStream(runId) {
  if (!state.runConfigId || state.selectedRunId !== runId) return;
  if (state.runEventSource && state.runEventRunId === runId) return;
  stopPolling();
  stopRunStream("正在连接…", "connecting");
  if (!("EventSource" in window)) {
    setRunStreamState("轮询更新", "fallback");
    schedulePolling(runId);
    return;
  }
  const configId = state.runConfigId;
  const path = `/api/run-configs/${encodeURIComponent(configId)}/runs/${encodeURIComponent(runId)}/events`;
  const source = new EventSource(path);
  state.runEventSource = source;
  state.runEventRunId = runId;
  source.onopen = () => {
    if (state.runEventSource === source) setRunStreamState("实时更新", "live");
  };
  source.onmessage = (event) => {
    if (
      state.runEventSource !== source
      || state.selectedRunId !== runId
      || state.runConfigId !== configId
    ) return;
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (_error) {
      stopRunStream("流数据错误，轮询中", "fallback");
      schedulePolling(runId);
      return;
    }
    renderRunProgress(payload);
    if (!runIsActive(payload.status)) {
      stopRunStream("已结束");
      stopPolling();
      loadRunStatus(runId);
    }
  };
  source.onerror = () => {
    if (state.runEventSource !== source || state.selectedRunId !== runId) return;
    stopRunStream("连接中断，轮询中", "fallback");
    schedulePolling(runId);
  };
}

async function loadRunStatus(runId) {
  if (!state.runConfigId || !runId) return;
  try {
    const payload = await api(
      `/api/run-configs/${encodeURIComponent(state.runConfigId)}/runs/${encodeURIComponent(runId)}`,
    );
    if (state.selectedRunId !== runId) return;
    renderRunDetail(payload);
    renderRunsTable(await api(`/api/run-configs/${encodeURIComponent(state.runConfigId)}/runs`));
    if (runIsActive(payload.status)) startRunStream(runId);
    else {
      stopPolling();
      stopRunStream("已结束");
    }
  } catch (error) {
    setRunNotice(error.message, true);
  }
}

async function resumeRun() {
  if (!state.runConfigId || !state.selectedRunId) return;
  if (!runSourceReady()) {
    renderRunPreview();
    return;
  }
  if (usingBrowserProfile()) flushApiHistorySave(true);
  $("#resume-run").disabled = true;
  try {
    await api(
      `/api/run-configs/${encodeURIComponent(state.runConfigId)}/runs/${encodeURIComponent(state.selectedRunId)}/resume`,
      { method: "POST", body: JSON.stringify(currentRunRequest()) },
    );
    setRunNotice(`Run ${state.selectedRunId} 正在恢复`);
    await loadRunStatus(state.selectedRunId);
  } catch (error) {
    setRunNotice(error.message, true);
  } finally {
    $("#resume-run").disabled = false;
  }
}

async function stopRun() {
  if (!state.runConfigId || !state.selectedRunId) return;
  if (!window.confirm(`确定中断 Run ${state.selectedRunId}？已完成结果会保留，可稍后恢复。`)) {
    return;
  }
  const button = $("#stop-run");
  button.disabled = true;
  button.textContent = "中断中…";
  setRunNotice(`正在中断 Run ${state.selectedRunId}…`);
  try {
    const payload = await api(
      `/api/run-configs/${encodeURIComponent(state.runConfigId)}/runs/${encodeURIComponent(state.selectedRunId)}/stop`,
      { method: "POST" },
    );
    setRunNotice(`Run ${payload.run_id} 已中断；已完成结果已保留`);
    await refreshRuns();
    await loadRunStatus(payload.run_id);
  } catch (error) {
    setRunNotice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function deleteRun() {
  if (!state.runConfigId || !state.selectedRunId) return;
  const runId = state.selectedRunId;
  if (!window.confirm(
    `确定永久删除 Run ${runId}？SQLite 记录、请求产物和报告都会被删除，且无法恢复。`,
  )) return;
  const button = $("#delete-run");
  button.disabled = true;
  setRunNotice(`正在删除 Run ${runId}…`);
  try {
    const payload = await api(
      `/api/run-configs/${encodeURIComponent(state.runConfigId)}/runs/${encodeURIComponent(runId)}`,
      { method: "DELETE" },
    );
    stopPolling();
    stopRunStream();
    state.selectedRunId = null;
    $("#run-detail").hidden = true;
    $("#run-detail-empty").hidden = false;
    $("#selected-run-id").textContent = "选择一个 Run";
    const cleanupNote = payload.cleanup_errors?.length
      ? `；有 ${payload.cleanup_errors.length} 个临时目录清理失败`
      : "";
    setRunNotice(`Run ${runId} 已删除${cleanupNote}`, Boolean(payload.cleanup_errors?.length));
    await refreshRuns();
    $("#start-run").disabled = !runSourceReady();
  } catch (error) {
    setRunNotice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function bindEvents() {
  window.addEventListener("beforeunload", () => {
    flushApiHistorySave();
    if (state.runEventSource) state.runEventSource.close();
  });
  document.addEventListener("pointermove", handlePointerMove, { passive: false });
  document.addEventListener("pointerup", (event) => {
    const drag = state.pointerDrag;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (event.button !== 0 && event.pointerType !== "touch") return;
    if (drag.active) {
      const cell = boardCellAt(event.clientX, event.clientY);
      if (cell) updateDragPreview(Number(cell.dataset.row), Number(cell.dataset.col));
      else clearDragPreview();
    }
    finishPointerDrag(true);
  });
  document.addEventListener("pointercancel", (event) => {
    if (state.pointerDrag?.pointerId === event.pointerId) finishPointerDrag(false);
  });
  document.addEventListener("mouseup", (event) => {
    if (event.button !== 0 || !state.pointerDrag?.active) return;
    const cell = boardCellAt(event.clientX, event.clientY);
    if (cell) updateDragPreview(Number(cell.dataset.row), Number(cell.dataset.col));
    else clearDragPreview();
    finishPointerDrag(true);
  });
  document.addEventListener("mousedown", (event) => {
    if (event.button !== 2 || !state.pointerDrag?.active) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    state.suppressContextMenuUntil = Date.now() + 1000;
    rotateActiveDrag();
  }, true);
  document.addEventListener("contextmenu", (event) => {
    if (!state.pointerDrag?.active && Date.now() >= state.suppressContextMenuUntil) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  $("#suite-select").addEventListener("change", (event) => {
    state.suiteId = event.target.value;
    populateScenarioSelect();
  });
  $("#scenario-select").addEventListener("change", (event) => {
    state.scenarioId = event.target.value;
    loadScenario();
  });
  $("#rotation-select").addEventListener("change", (event) => {
    state.selectedRotation = Number(event.target.value);
    const existing = state.placements.get(state.selectedItemId);
    if (existing) {
      existing.rotation = state.selectedRotation;
      state.placements.set(existing.item_id, existing);
      renderLayout();
      evaluateLayout();
    } else {
      renderRotationControl();
    }
  });
  $("#load-oracle").addEventListener("click", loadOracle);
  $("#clear-layout").addEventListener("click", clearLayout);
  $("#run-config-select").addEventListener("change", (event) => {
    state.runConfigId = event.target.value;
    loadRunConfig();
  });
  $("#model-source-select").addEventListener("change", () => {
    flushApiHistorySave();
    syncModelSource();
  });
  $("#api-history-select").addEventListener("change", (event) => {
    selectApiHistory(event.target.value);
  });
  $("#new-api-profile").addEventListener("click", newApiHistory);
  $("#delete-api-profile").addEventListener("click", deleteApiHistory);
  $("#toggle-api-key").addEventListener("click", () => {
    const input = $("#api-key");
    const visible = input.type === "text";
    input.type = visible ? "password" : "text";
    $("#toggle-api-key").textContent = visible ? "显示" : "隐藏";
    $("#toggle-api-key").setAttribute("aria-label", visible ? "显示 API Key" : "隐藏 API Key");
  });
  $("#api-profile-fields").addEventListener("input", scheduleApiHistorySave);
  $("#api-profile-fields").addEventListener("change", (event) => {
    if (event.target.id === "api-protocol") syncProtocolFields();
    scheduleApiHistorySave();
  });
  $("#start-run").addEventListener("click", startRun);
  $("#refresh-runs").addEventListener("click", refreshRuns);
  $("#stop-run").addEventListener("click", stopRun);
  $("#resume-run").addEventListener("click", resumeRun);
$("#delete-run").addEventListener("click", deleteRun);
$("#visual-sheet-mode").addEventListener("change", () => {
  renderVisualScenarioInput();
});
}

setupTabs();
bindEvents();
loadApiHistory();
syncModelSource();
loadSuites();
