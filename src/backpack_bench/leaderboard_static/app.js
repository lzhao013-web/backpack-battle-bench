"use strict";

const page = {
  data: null,
  suiteId: null,
  selectedProfile: null,
  selectedScenarioId: null,
  sortKey: "overall_score",
  sortDirection: "desc",
};

const play = {
  scenario: null,
  placements: new Map(),
  selectedItemId: null,
  selectedRotation: 0,
  pointer: null,
  draggingItemId: null,
  dragOffset: [0, 0],
  grabIndex: null,
  anchor: null,
  ghost: null,
  suppressClickUntil: 0,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function formatPercent(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function formatScore(value) {
  return Number(value || 0).toFixed(2);
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function formatLatency(value) {
  if (value == null) return "—";
  return value < 1000 ? `${Number(value).toFixed(0)} ms` : `${(Number(value) / 1000).toFixed(1)} s`;
}

function currentSuite() {
  return page.data?.suites.find((suite) => suite.id === page.suiteId) || null;
}

function currentTrack() {
  const suite = currentSuite();
  const mode = $("#track-select").value;
  return page.data?.leaderboard_tracks.find((track) => (
    track.suite_id === suite?.id
    && track.suite_hash === suite?.suite_hash
    && track.prompt_mode === mode
  )) || { entries: [] };
}

function summaryCard(label, value, note = "") {
  const card = document.createElement("div");
  card.className = "summary-card";
  const labelNode = document.createElement("span");
  labelNode.textContent = label;
  const valueNode = document.createElement("strong");
  valueNode.textContent = value;
  card.append(labelNode, valueNode);
  if (note) {
    const noteNode = document.createElement("span");
    noteNode.textContent = note;
    card.append(noteNode);
  }
  return card;
}

function sortedEntries() {
  const entries = [...(currentTrack().entries || [])];
  const value = (entry) => {
    if (page.sortKey === "rank") return entry.official_rank;
    if (page.sortKey === "model") return entry.model || "";
    return entry[page.sortKey];
  };
  entries.sort((left, right) => (
    Number(Boolean(right.eligible)) - Number(Boolean(left.eligible))
    || compareSortValues(value(left), value(right), page.sortDirection)
    || String(left.profile_hash).localeCompare(String(right.profile_hash))
  ));
  return entries;
}

function compareSortValues(left, right, direction) {
  const leftMissing = left == null || left === "";
  const rightMissing = right == null || right === "";
  if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
  if (leftMissing) return 0;
  const multiplier = direction === "asc" ? 1 : -1;
  if (typeof left === "number" || typeof right === "number") {
    return (Number(left) - Number(right)) * multiplier;
  }
  return String(left).localeCompare(String(right), "zh-CN", { numeric: true }) * multiplier;
}

function renderSortHeaders() {
  $$(".sort-button").forEach((button) => {
    const active = button.dataset.sort === page.sortKey;
    button.classList.toggle("is-active", active);
    button.querySelector("span").textContent = active
      ? (page.sortDirection === "asc" ? "↑" : "↓")
      : "↕";
    button.closest("th").setAttribute(
      "aria-sort",
      active ? (page.sortDirection === "asc" ? "ascending" : "descending") : "none",
    );
  });
}

function renderSummary() {
  const suite = currentSuite();
  const entries = currentTrack().entries || [];
  const eligible = entries.filter((entry) => entry.eligible);
  const top = [...eligible].sort((a, b) => b.overall_score - a.overall_score)[0];
  const valid = [...eligible].sort((a, b) => b.valid_rate - a.valid_rate)[0];
  const container = $("#summary");
  container.replaceChildren(
    summaryCard("当前第一", top?.model || "暂无", top ? `${formatScore(top.overall_score)} 分` : "等待完整 Run"),
    summaryCard("最高合法率", valid?.model || "暂无", valid ? formatPercent(valid.valid_rate) : "—"),
    summaryCard("正式入榜", String(eligible.length), `共 ${entries.length} 个公开配置`),
    summaryCard("公开题目", String(suite?.scenarios.length || 0), suite?.title || "—"),
  );
  $("#entry-count").textContent = `${eligible.length} 个入榜配置`;
  $("#version-note").textContent = suite
    ? `${suite.title} · ${$("#track-select").selectedOptions[0].textContent} · suite ${suite.suite_hash.slice(0, 12)}…`
    : "";
}

function renderLeaderboard() {
  renderSummary();
  renderSortHeaders();
  const entries = sortedEntries();
  const body = $("#leaderboard-body");
  body.replaceChildren();
  $("#leaderboard-empty").hidden = entries.length > 0;
  entries.forEach((entry) => {
    const row = document.createElement("tr");
    row.classList.toggle("is-experimental", !entry.eligible);
    row.classList.toggle("is-selected", page.selectedProfile === entry.profile_hash);
    const rank = document.createElement("td");
    rank.className = "rank";
    rank.textContent = entry.official_rank == null ? "—" : `#${entry.official_rank}`;
    if (!entry.eligible) {
      rank.className = "experimental-note";
      rank.textContent = "未入榜";
      rank.title = entry.eligibility_reasons.join("；");
    }
    const model = document.createElement("td");
    model.className = "model-cell";
    const modelName = document.createElement("strong");
    modelName.textContent = entry.model;
    const modelMeta = document.createElement("span");
    modelMeta.textContent = `${entry.display_name || entry.profile_id} · ${entry.protocol}`;
    model.append(modelName, modelMeta);
    const values = [
      entry.thinking_effort || "default",
      formatScore(entry.overall_score),
      formatScore(entry.best_of_3_score),
      formatPercent(entry.valid_rate),
      formatPercent(entry.optimal_hit_rate),
      formatLatency(entry.latency_p50_ms),
      Number(entry.output_tokens || 0).toLocaleString("zh-CN"),
      entry.estimated_cost == null ? "—" : `$${Number(entry.estimated_cost).toFixed(3)}`,
      formatDate(entry.completed_at),
    ];
    row.append(rank, model);
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if ([1, 2].includes(index)) cell.className = "score-value";
      row.append(cell);
    });
    row.addEventListener("click", () => showModelDetail(entry));
    body.append(row);
  });
  if (page.selectedProfile && !entries.some((entry) => entry.profile_hash === page.selectedProfile)) {
    closeModelDetail();
  }
}

function showModelDetail(entry) {
  page.selectedProfile = entry.profile_hash;
  $("#model-detail").hidden = false;
  $("#detail-title").textContent = entry.model;
  $("#detail-subtitle").textContent = `${entry.display_name || entry.profile_id} · ${entry.thinking_effort || "default"} · Run ${entry.run_id}`;
  $("#detail-metrics").replaceChildren(
    summaryCard("平均分", formatScore(entry.overall_score)),
    summaryCard("Best-of-3", formatScore(entry.best_of_3_score)),
    summaryCard("合法率", formatPercent(entry.valid_rate)),
    summaryCard("最优命中", formatPercent(entry.optimal_hit_rate)),
    summaryCard("延迟 P95", formatLatency(entry.latency_p95_ms)),
  );
  const groups = $("#level-groups");
  groups.replaceChildren();
  (entry.groups?.difficulty || []).forEach((group) => {
    const card = document.createElement("div");
    card.className = "level-card";
    const label = document.createElement("span");
    label.textContent = group.group_value;
    const score = document.createElement("strong");
    score.textContent = formatScore(group.overall_score);
    const meta = document.createElement("span");
    meta.textContent = `Best3 ${formatScore(group.best_of_3_score)} · 合法 ${formatPercent(group.valid_rate)}`;
    card.append(label, score, meta);
    groups.append(card);
  });
  const scores = $("#scenario-scores");
  scores.replaceChildren();
  (entry.scenario_results || []).forEach((scenario) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "scenario-score";
    card.style.setProperty("--score", String(Math.max(0, Math.min(100, scenario.ratio_mean * 100))));
    const name = document.createElement("strong");
    name.textContent = scenario.title;
    const score = document.createElement("span");
    score.textContent = `平均 ${formatPercent(scenario.ratio_mean)} · Best3 ${formatPercent(scenario.ratio_best_of_3)}`;
    const meta = document.createElement("span");
    meta.textContent = `攻击 ${Number(scenario.attack_mean).toFixed(1)} / ${scenario.oracle_attack} · 合法 ${formatPercent(scenario.valid_rate)}`;
    card.append(name, score, meta);
    card.addEventListener("click", () => {
      const target = currentSuite()?.scenarios.find((item) => item.id === scenario.scenario_id);
      if (target) openPlayground(target);
    });
    scores.append(card);
  });
  renderLeaderboard();
  $("#model-detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeModelDetail() {
  page.selectedProfile = null;
  $("#model-detail").hidden = true;
  renderLeaderboard();
}

function populateDifficulties() {
  const select = $("#difficulty-select");
  const current = select.value || "all";
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = "全部";
  select.append(all);
  const values = [...new Set((currentSuite()?.scenarios || []).map((item) => item.difficulty))].sort();
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  });
  select.value = values.includes(current) ? current : "all";
}

function renderScenarioGallery() {
  const suite = currentSuite();
  const difficulty = $("#difficulty-select").value;
  const mode = $("#preview-mode").value;
  const gallery = $("#scenario-gallery");
  gallery.replaceChildren();
  const scenarios = (suite?.scenarios || []).filter((scenario) => (
    difficulty === "all" || scenario.difficulty === difficulty
  ));
  if (!scenarios.length) {
    const empty = document.createElement("div");
    empty.className = "empty panel";
    empty.textContent = "当前筛选条件下没有题目";
    gallery.append(empty);
    return;
  }
  if (!scenarios.some((scenario) => scenario.id === page.selectedScenarioId)) {
    page.selectedScenarioId = scenarios[0].id;
  }
  const selected = scenarios.find((scenario) => scenario.id === page.selectedScenarioId);

  const index = document.createElement("nav");
  index.className = "scenario-index panel";
  index.setAttribute("aria-label", "题目目录");
  const indexHeader = document.createElement("div");
  indexHeader.className = "scenario-index-header";
  const indexTitle = document.createElement("strong");
  indexTitle.textContent = "题目目录";
  const count = document.createElement("span");
  count.className = "badge";
  count.textContent = `${scenarios.length} 题`;
  indexHeader.append(indexTitle, count);
  index.append(indexHeader);

  const groups = new Map();
  scenarios.forEach((scenario) => {
    if (!groups.has(scenario.difficulty)) groups.set(scenario.difficulty, []);
    groups.get(scenario.difficulty).push(scenario);
  });
  groups.forEach((items, level) => {
    const group = document.createElement("section");
    group.className = "scenario-index-group";
    const heading = document.createElement("h3");
    heading.textContent = level;
    group.append(heading);
    items.forEach((scenario) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "scenario-index-item";
      button.classList.toggle("is-selected", scenario.id === selected.id);
      button.setAttribute("aria-current", scenario.id === selected.id ? "true" : "false");
      const number = document.createElement("span");
      number.className = "scenario-number";
      number.textContent = String((suite?.scenarios.indexOf(scenario) || 0) + 1).padStart(2, "0");
      const copy = document.createElement("span");
      const title = document.createElement("strong");
      title.textContent = scenario.title;
      const facts = document.createElement("small");
      facts.textContent = `${scenario.board.height}×${scenario.board.width} · ${scenario.instances.length} 件 · Oracle ${scenario.oracle_attack}`;
      copy.append(title, facts);
      button.append(number, copy);
      button.addEventListener("click", () => {
        page.selectedScenarioId = scenario.id;
        renderScenarioGallery();
      });
      group.append(button);
    });
    index.append(group);
  });

  const preview = document.createElement("article");
  preview.className = "scenario-preview panel";
  let previewContent;
  if (mode === "text") {
    preview.classList.add("is-text");
    previewContent = document.createElement("div");
    previewContent.className = "scenario-text-preview";
    const prompt = document.createElement("pre");
    prompt.textContent = selected.text_prompt;
    previewContent.append(prompt);
  } else {
    previewContent = document.createElement("a");
    previewContent.className = "scenario-preview-image";
    previewContent.href = selected.sheets[mode];
    previewContent.target = "_blank";
    previewContent.rel = "noopener";
    const image = document.createElement("img");
    image.src = selected.sheets[mode];
    image.alt = `${selected.title} 题面`;
    previewContent.append(image);
  }
  const body = document.createElement("div");
  body.className = "scenario-preview-body";
  const meta = document.createElement("div");
  meta.className = "scenario-meta";
  [selected.difficulty, ...selected.tags].forEach((value) => {
    const tag = document.createElement("span");
    tag.textContent = value;
    meta.append(tag);
  });
  const title = document.createElement("h3");
  title.textContent = selected.title;
  const facts = document.createElement("div");
  facts.className = "scenario-facts";
  facts.textContent = `${selected.board.height}×${selected.board.width} 背包 · ${selected.instances.length} 件物品 · Oracle ${selected.oracle_attack}`;
  const actions = document.createElement("div");
  actions.className = "scenario-actions";
  const playButton = document.createElement("button");
  playButton.type = "button";
  playButton.className = "play-button";
  playButton.textContent = "打开试玩";
  playButton.addEventListener("click", () => openPlayground(selected));
  const full = document.createElement("a");
  full.href = mode === "text" ? selected.text_prompt_url : selected.sheets[mode];
  full.target = "_blank";
  full.rel = "noopener";
  full.textContent = mode === "text" ? "新窗口查看文字" : "新窗口查看题面";
  actions.append(playButton, full);
  body.append(meta, title, facts, actions);
  preview.append(previewContent, body);
  gallery.append(index, preview);
}

function rotateShapeWithIndices(shape, rotation) {
  const turns = ((rotation % 360) + 360) % 360 / 90;
  const values = shape.map(([sourceRow, sourceCol], index) => {
    let row = sourceRow;
    let col = sourceCol;
    for (let turn = 0; turn < turns; turn += 1) [row, col] = [col, -row];
    return [row, col, index];
  });
  const minRow = Math.min(...values.map(([row]) => row));
  const minCol = Math.min(...values.map(([, col]) => col));
  return values.map(([row, col, index]) => [row - minRow, col - minCol, index]);
}

function rotatedShape(shape, rotation) {
  return rotateShapeWithIndices(shape, rotation).map(([row, col]) => [row, col]);
}

function rotateVector(vector, rotation) {
  let [row, col] = vector;
  const turns = ((rotation % 360) + 360) % 360 / 90;
  for (let turn = 0; turn < turns; turn += 1) [row, col] = [col, -row];
  return [row, col];
}

function playInstance(itemId) {
  return play.scenario?.instances.find((item) => item.item_id === itemId) || null;
}

function occupiedCells(instance, placement) {
  return rotatedShape(instance.shape, placement.rotation).map(([row, col]) => [
    placement.row + row,
    placement.col + col,
  ]);
}

function analyzePlay() {
  const valid = new Set(play.scenario.valid_cells.map(([row, col]) => `${row},${col}`));
  const owners = new Map();
  const invalid = new Set();
  for (const placement of play.placements.values()) {
    const instance = playInstance(placement.item_id);
    if (!instance || !instance.rotations.includes(placement.rotation)) {
      invalid.add(placement.item_id);
      continue;
    }
    occupiedCells(instance, placement).forEach(([row, col]) => {
      const key = `${row},${col}`;
      if (!valid.has(key)) invalid.add(placement.item_id);
      if (owners.has(key)) {
        invalid.add(placement.item_id);
        invalid.add(owners.get(key));
      } else {
        owners.set(key, placement.item_id);
      }
    });
  }
  return { valid, owners, invalid };
}

function evaluatePlay() {
  const analysis = analyzePlay();
  const stats = new Map();
  for (const placement of play.placements.values()) {
    const instance = playInstance(placement.item_id);
    if (instance) stats.set(placement.item_id, { ...instance.stats });
  }
  let events = 0;
  if (!analysis.invalid.size) {
    const placements = [...play.placements.values()].sort((a, b) => a.item_id.localeCompare(b.item_id));
    placements.forEach((placement) => {
      const source = playInstance(placement.item_id);
      if (!source) return;
      const sourceCells = occupiedCells(source, placement).sort();
      (source.effects || []).forEach((effect) => {
        const config = effect.config || {};
        const targets = [];
        if (effect.type === "adjacent_stat_bonus") {
          const directions = config.directions || [[0, -1], [0, 1]];
          sourceCells.forEach(([sourceRow, sourceCol]) => {
            directions.forEach((direction) => {
              const [deltaRow, deltaCol] = rotateVector(direction, placement.rotation);
              const target = analysis.owners.get(`${sourceRow + deltaRow},${sourceCol + deltaCol}`);
              if (target && target !== placement.item_id && playInstance(target)?.category === (config.target_category || "weapon")) targets.push(target);
            });
          });
        } else if (effect.type === "ray_stat_bonus") {
          const [deltaRow, deltaCol] = rotateVector(config.direction || [-1, 0], placement.rotation);
          sourceCells.forEach(([sourceRow, sourceCol]) => {
            let row = sourceRow + deltaRow;
            let col = sourceCol + deltaCol;
            while (analysis.valid.has(`${row},${col}`)) {
              const target = analysis.owners.get(`${row},${col}`);
              if (target && target !== placement.item_id) {
                if (playInstance(target)?.category === (config.target_category || "weapon")) targets.push(target);
                if (config.blocked === true) break;
              }
              row += deltaRow;
              col += deltaCol;
            }
          });
        }
        const selected = config.once_per_target === false ? targets : [...new Set(targets)].sort();
        selected.forEach((target) => {
          const current = stats.get(target);
          const stat = config.stat || "attack";
          if (current) current[stat] = Number(current[stat] || 0) + Number(config.amount || 0);
          events += 1;
        });
      });
    });
  }
  const objective = play.scenario.objective.config || {};
  let attack = 0;
  if (!analysis.invalid.size) {
    for (const [itemId, itemStats] of stats.entries()) {
      if (playInstance(itemId)?.category === (objective.category || "weapon")) {
        attack += Number(itemStats[objective.stat || "attack"] || 0);
      }
    }
  }
  const oracle = Number(play.scenario.oracle_attack || 0);
  const ratio = oracle ? attack / oracle : 0;
  $("#play-score").textContent = String(attack);
  $("#play-oracle").textContent = String(oracle);
  $("#play-score-fill").style.width = `${Math.min(100, ratio * 100)}%`;
  const status = $("#play-status");
  status.className = analysis.invalid.size ? "play-status is-error" : "play-status is-valid";
  status.textContent = analysis.invalid.size
    ? `布局不合法：${[...analysis.invalid].join("、")}`
    : attack === oracle ? "已命中精确最优" : `合法布局 · ${events} 次效果 · ${(ratio * 100).toFixed(1)}% Oracle`;
  return analysis;
}

function renderPlayInventory() {
  const list = $("#play-items");
  list.replaceChildren();
  play.scenario.instances.forEach((instance) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "item-button";
    button.classList.toggle("is-selected", play.selectedItemId === instance.item_id);
    button.classList.toggle("is-placed", play.placements.has(instance.item_id));
    const rotation = play.placements.get(instance.item_id)?.rotation
      ?? (play.selectedItemId === instance.item_id ? play.selectedRotation : instance.rotations[0]);
    const image = document.createElement("img");
    image.src = instance.images[String(rotation)];
    image.alt = "";
    image.draggable = false;
    const name = document.createElement("span");
    name.textContent = `${instance.display_name} · ${instance.item_id}`;
    button.append(image, name);
    button.addEventListener("click", () => selectPlayItem(instance.item_id));
    button.addEventListener("pointerdown", (event) => armDrag(instance.item_id, [0, 0], event));
    button.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      cyclePlayRotation(instance.item_id);
    });
    list.append(button);
  });
  $("#play-count").textContent = `${play.placements.size} / ${play.scenario.instances.length}`;
  const selected = playInstance(play.selectedItemId);
  $("#play-selection").textContent = selected
    ? `${selected.display_name} · ${play.selectedRotation}° · 右键旋转`
    : "请选择物品";
}

function renderPlayBoard() {
  const board = $("#play-board");
  board.replaceChildren();
  board.style.setProperty("--cols", play.scenario.board.width);
  const analysis = analyzePlay();
  for (let row = 0; row < play.scenario.board.height; row += 1) {
    for (let col = 0; col < play.scenario.board.width; col += 1) {
      const key = `${row},${col}`;
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "board-cell";
      cell.dataset.row = String(row);
      cell.dataset.col = String(col);
      const coordinate = document.createElement("span");
      coordinate.className = "cell-coordinate";
      coordinate.textContent = `${row},${col}`;
      cell.append(coordinate);
      if (!analysis.valid.has(key)) {
        cell.classList.add("is-hole");
        cell.disabled = true;
        cell.append("×");
      } else {
        const ownerId = analysis.owners.get(key);
        const owner = playInstance(ownerId);
        const placement = ownerId ? play.placements.get(ownerId) : null;
        if (owner && placement) {
          const shape = rotatedShape(owner.shape, placement.rotation);
          const local = shape.find(([localRow, localCol]) => placement.row + localRow === row && placement.col + localCol === col);
          const height = Math.max(...shape.map(([localRow]) => localRow)) + 1;
          const width = Math.max(...shape.map(([, localCol]) => localCol)) + 1;
          if (local) {
            const viewport = document.createElement("span");
            viewport.className = "cell-image-viewport";
            const image = document.createElement("img");
            image.className = "cell-image";
            image.src = owner.images[String(placement.rotation)];
            image.alt = "";
            image.draggable = false;
            image.style.width = `${width * 100}%`;
            image.style.height = `${height * 100}%`;
            image.style.left = `${-local[1] * 100}%`;
            image.style.top = `${-local[0] * 100}%`;
            viewport.append(image);
            cell.append(viewport);
          }
          const suffix = document.createElement("span");
          suffix.className = "cell-instance";
          suffix.textContent = `#${ownerId.split("_").at(-1)}`;
          cell.append(suffix);
          cell.addEventListener("pointerdown", (event) => armDrag(
            ownerId,
            [row - placement.row, col - placement.col],
            event,
          ));
        }
        cell.classList.toggle("is-conflict", analysis.invalid.has(ownerId));
        cell.addEventListener("click", () => {
          if (Date.now() >= play.suppressClickUntil) placeSelected(row, col);
        });
        cell.addEventListener("contextmenu", (event) => {
          event.preventDefault();
          if (ownerId) cyclePlayRotation(ownerId);
          else if (play.selectedItemId) cyclePlayRotation(play.selectedItemId);
        });
      }
      board.append(cell);
    }
  }
}

function renderPlay() {
  renderPlayInventory();
  renderPlayBoard();
  evaluatePlay();
}

function selectPlayItem(itemId) {
  play.selectedItemId = itemId;
  const instance = playInstance(itemId);
  play.selectedRotation = play.placements.get(itemId)?.rotation ?? instance.rotations[0];
  renderPlayInventory();
}

function placeSelected(row, col) {
  if (!play.selectedItemId) return;
  play.placements.set(play.selectedItemId, {
    item_id: play.selectedItemId,
    row,
    col,
    rotation: play.selectedRotation,
  });
  renderPlay();
}

function cyclePlayRotation(itemId) {
  const instance = playInstance(itemId);
  if (!instance) return;
  if (play.pointer?.active && play.draggingItemId === itemId) {
    rotateActiveDrag();
    return;
  }
  const existing = play.placements.get(itemId);
  const current = existing?.rotation ?? (play.selectedItemId === itemId ? play.selectedRotation : instance.rotations[0]);
  const next = instance.rotations[(instance.rotations.indexOf(current) + 1) % instance.rotations.length];
  play.selectedItemId = itemId;
  play.selectedRotation = next;
  if (existing) play.placements.set(itemId, { ...existing, rotation: next });
  renderPlay();
}

function createGhost(instance, rotation) {
  const shape = rotatedShape(instance.shape, rotation);
  const height = Math.max(...shape.map(([row]) => row)) + 1;
  const width = Math.max(...shape.map(([, col]) => col)) + 1;
  const ghost = document.createElement("div");
  ghost.className = "drag-ghost";
  const image = document.createElement("img");
  image.src = instance.images[String(rotation)];
  image.style.width = `${width * 30}px`;
  image.style.height = `${height * 30}px`;
  image.draggable = false;
  ghost.append(image);
  document.body.append(ghost);
  return ghost;
}

function positionGhost(x, y) {
  if (!play.ghost) return;
  play.ghost.style.left = `${x - (play.dragOffset[1] * 30 + 19)}px`;
  play.ghost.style.top = `${y - (play.dragOffset[0] * 30 + 19)}px`;
}

function armDrag(itemId, offset, event) {
  if (event.button !== 0) return;
  const instance = playInstance(itemId);
  if (!instance) return;
  const rotation = play.placements.get(itemId)?.rotation
    ?? (play.selectedItemId === itemId ? play.selectedRotation : instance.rotations[0]);
  const indexed = rotateShapeWithIndices(instance.shape, rotation);
  const grabbed = indexed.find(([row, col]) => row === offset[0] && col === offset[1]) || indexed[0];
  play.pointer = {
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
  play.dragOffset = [grabbed[0], grabbed[1]];
  play.grabIndex = grabbed[2];
  event.currentTarget.setPointerCapture?.(event.pointerId);
}

function activateDrag() {
  if (!play.pointer || play.pointer.active) return;
  const instance = playInstance(play.pointer.itemId);
  play.pointer.active = true;
  play.draggingItemId = play.pointer.itemId;
  play.selectedItemId = play.pointer.itemId;
  play.selectedRotation = play.placements.get(play.pointer.itemId)?.rotation ?? play.pointer.rotation;
  play.ghost = createGhost(instance, play.selectedRotation);
  document.body.classList.add("is-dragging");
  positionGhost(play.pointer.lastX, play.pointer.lastY);
}

function boardCellAt(x, y) {
  const target = document.elementFromPoint(x, y);
  const cell = target?.closest?.("#play-board .board-cell");
  return cell && !cell.disabled ? cell : null;
}

function clearDragPreview() {
  $$("#play-board .is-drop-valid, #play-board .is-drop-invalid").forEach((cell) => {
    cell.classList.remove("is-drop-valid", "is-drop-invalid");
  });
  play.anchor = null;
}

function updateDragPreview(targetRow, targetCol) {
  clearDragPreview();
  const instance = playInstance(play.draggingItemId);
  if (!instance) return;
  const row = targetRow - play.dragOffset[0];
  const col = targetCol - play.dragOffset[1];
  play.anchor = [row, col];
  const candidate = { item_id: instance.item_id, row, col, rotation: play.selectedRotation };
  const analysis = analyzePlay();
  const occupiedByOthers = new Set();
  for (const placement of play.placements.values()) {
    if (placement.item_id === instance.item_id) continue;
    occupiedCells(playInstance(placement.item_id), placement).forEach(([r, c]) => occupiedByOthers.add(`${r},${c}`));
  }
  const cells = occupiedCells(instance, candidate);
  const valid = cells.every(([r, c]) => analysis.valid.has(`${r},${c}`) && !occupiedByOthers.has(`${r},${c}`));
  cells.forEach(([r, c]) => {
    const cell = document.querySelector(`#play-board .board-cell[data-row="${r}"][data-col="${c}"]`);
    cell?.classList.add(valid ? "is-drop-valid" : "is-drop-invalid");
  });
  $("#play-message").className = valid ? "play-message" : "play-message is-error";
  $("#play-message").textContent = valid ? `释放以放置 · 锚点 (${row},${col})` : "此处会越界或重叠";
}

function rotateActiveDrag() {
  const instance = playInstance(play.draggingItemId);
  if (!play.pointer?.active || !instance) return;
  const index = instance.rotations.indexOf(play.selectedRotation);
  play.selectedRotation = instance.rotations[(index + 1) % instance.rotations.length];
  const grabbed = rotateShapeWithIndices(instance.shape, play.selectedRotation)
    .find(([, , sourceIndex]) => sourceIndex === play.grabIndex);
  if (grabbed) play.dragOffset = [grabbed[0], grabbed[1]];
  play.ghost?.remove();
  play.ghost = createGhost(instance, play.selectedRotation);
  positionGhost(play.pointer.lastX, play.pointer.lastY);
  const cell = boardCellAt(play.pointer.lastX, play.pointer.lastY);
  if (cell) updateDragPreview(Number(cell.dataset.row), Number(cell.dataset.col));
}

function finishDrag(commit) {
  if (!play.pointer) return;
  const active = play.pointer.active;
  if (active && commit && play.anchor && play.draggingItemId) {
    play.placements.set(play.draggingItemId, {
      item_id: play.draggingItemId,
      row: play.anchor[0],
      col: play.anchor[1],
      rotation: play.selectedRotation,
    });
  }
  play.pointer.source?.releasePointerCapture?.(play.pointer.pointerId);
  if (active) play.suppressClickUntil = Date.now() + 180;
  play.ghost?.remove();
  play.ghost = null;
  play.pointer = null;
  play.draggingItemId = null;
  play.anchor = null;
  document.body.classList.remove("is-dragging");
  clearDragPreview();
  if (active) renderPlay();
}

function openPlayground(scenario) {
  play.scenario = scenario;
  play.placements = new Map();
  play.selectedItemId = scenario.instances[0]?.item_id || null;
  play.selectedRotation = scenario.instances[0]?.rotations[0] || 0;
  $("#play-title").textContent = scenario.title;
  $("#play-meta").textContent = `${scenario.difficulty} · ${scenario.board.height}×${scenario.board.width} · Oracle ${scenario.oracle_attack}`;
  $("#play-sheet").src = scenario.sheets.visual_full;
  $("#play-message").textContent = "选择物品后点击格子，或把物品直接拖入背包";
  renderPlay();
  $("#play-dialog").showModal();
}

function closePlayground() {
  finishDrag(false);
  $("#play-dialog").close();
}

function renderAll() {
  populateDifficulties();
  renderLeaderboard();
  renderScenarioGallery();
}

async function loadData() {
  try {
    const response = await fetch("./data.json");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    page.data = await response.json();
    const suiteSelect = $("#suite-select");
    page.data.suites.forEach((suite) => {
      const option = document.createElement("option");
      option.value = suite.id;
      option.textContent = `${suite.title} (${suite.scenarios.length})`;
      suiteSelect.append(option);
    });
    page.suiteId = page.data.suites.find((suite) => suite.id === "ladder-v2")?.id
      || page.data.suites[0]?.id || null;
    suiteSelect.value = page.suiteId || "";
    renderAll();
  } catch (error) {
    $("#leaderboard-empty").hidden = false;
    $("#leaderboard-empty").textContent = `排行榜数据加载失败：${error.message}`;
  }
}

function bindEvents() {
  $("#suite-select").addEventListener("change", (event) => {
    page.suiteId = event.target.value;
    page.selectedProfile = null;
    page.selectedScenarioId = null;
    closeModelDetail();
    renderAll();
  });
  $("#track-select").addEventListener("change", () => {
    page.selectedProfile = null;
    closeModelDetail();
    renderLeaderboard();
  });
  $$(".sort-button").forEach((button) => {
    button.addEventListener("click", () => {
      const key = button.dataset.sort;
      if (page.sortKey === key) {
        page.sortDirection = page.sortDirection === "asc" ? "desc" : "asc";
      } else {
        page.sortKey = key;
        page.sortDirection = ["model", "thinking_effort", "rank"].includes(key) ? "asc" : "desc";
      }
      renderLeaderboard();
    });
  });
  $("#difficulty-select").addEventListener("change", renderScenarioGallery);
  $("#preview-mode").addEventListener("change", renderScenarioGallery);
  $("#close-detail").addEventListener("click", closeModelDetail);
  $("#close-play").addEventListener("click", closePlayground);
  $("#clear-play").addEventListener("click", () => {
    play.placements.clear();
    renderPlay();
  });
  $("#load-witness").addEventListener("click", () => {
    const placements = play.scenario.oracle_witness?.placements || [];
    play.placements = new Map(placements.map((placement) => [placement.item_id, { ...placement }]));
    if (placements[0]) selectPlayItem(placements[0].item_id);
    renderPlay();
  });
  document.addEventListener("pointermove", (event) => {
    if (!play.pointer || play.pointer.pointerId !== event.pointerId) return;
    play.pointer.lastX = event.clientX;
    play.pointer.lastY = event.clientY;
    if (!play.pointer.active && Math.hypot(event.clientX - play.pointer.originX, event.clientY - play.pointer.originY) >= 5) activateDrag();
    if (!play.pointer.active) return;
    event.preventDefault();
    positionGhost(event.clientX, event.clientY);
    const cell = boardCellAt(event.clientX, event.clientY);
    if (cell) updateDragPreview(Number(cell.dataset.row), Number(cell.dataset.col));
    else clearDragPreview();
  }, { passive: false });
  document.addEventListener("pointerup", (event) => {
    if (play.pointer?.pointerId !== event.pointerId) return;
    finishDrag(Boolean(boardCellAt(event.clientX, event.clientY)));
  });
  document.addEventListener("pointercancel", (event) => {
    if (play.pointer?.pointerId === event.pointerId) finishDrag(false);
  });
  document.addEventListener("mousedown", (event) => {
    if (event.button !== 2 || !play.pointer?.active) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    rotateActiveDrag();
  }, true);
  document.addEventListener("contextmenu", (event) => {
    if (!play.pointer?.active) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
}

bindEvents();
loadData();
