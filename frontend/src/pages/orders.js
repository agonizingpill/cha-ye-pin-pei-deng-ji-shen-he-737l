import {esc, field, formData, formEnd, handle, header, memo, percent} from '../components.js';
import {GRADE, ORDER, request, store} from '../store.js';

function gradeOptions(selected = '') {
  return Object.entries(GRADE).map(([key, label]) =>
    `<option value="${key}" ${key === selected ? 'selected' : ''}>${label}</option>`).join('');
}

function suggestionCard(order) {
  const suggestion = order.suggestion;
  if (!suggestion) {
    const failure = order.failure || {};
    return `<section class="paper order-result"><h2>未找到达线配比</h2>
      <p class="empty">${esc(failure.message || '试算未给出建议。')}</p>
      ${failure.short_lots?.length ? `<h3>已品评、已封签余料余量</h3><ul class="stock-list">
        ${failure.short_lots.map(lot => `<li><strong>${esc(lot.lot)}</strong><span>可用 ${esc(lot.remaining)} kg</span></li>`).join('')}
      </ul>` : ''}
      <p class="muted">可调整目标等级或公斤数后重新试算。</p></section>`;
  }
  return `<section class="paper order-result"><h2>达线配比建议 <small>看建议不占料</small></h2>
    <div class="calibration-line"><span>目标等级 <strong>${GRADE[order.target_grade]}</strong></span>
      <span>订单重量 <strong>${esc(order.kilograms)} kg</strong></span>
      <span>建议置信度 <strong>${percent(suggestion.confidence)}</strong></span>
      <span>生效阈值 <strong>${percent(suggestion.effective_threshold)}</strong></span></div>
    <h3>建议用料</h3>
    <div class="ratio-band" aria-hidden="true">${suggestion.ingredients.map(row =>
      `<div style="flex:${Number(row.percent)}"></div>`).join('')}</div>
    <ul class="suggestion-list">${suggestion.ingredients.map((row, index) => {
      const lot = store.lots.find(item => item.id === row.lot_id);
      return `<li><strong>${esc(lot?.cultivar || '')}</strong><a href="#/lots/${row.lot_id}">${esc(row.lot)}</a>
        <span>配比 ${esc(row.percent)}%</span><span>用量 ${esc(suggestion.amounts[index])} kg</span>
        <small>当前可用 ${esc(lot?.available_kg ?? '-')} kg</small></li>`;
    }).join('')}</ul>
    <details><summary>模型概率与特征</summary>
      <div class="grade-bars">${Object.entries(GRADE).map(([key, label]) => `<div><span>${label}</span>
        <meter min="0" max="1" value="${suggestion.probabilities[key] ?? 0}">${percent(suggestion.probabilities[key] ?? 0)}</meter>
        <strong>${percent(suggestion.probabilities[key] ?? 0)}</strong></div>`).join('')}</div>
      <p class="mono">${Object.entries(suggestion.features).map(([key, value]) => `${key}=${value}`).join(' · ')}</p>
    </details></section>`;
}

function orderCard(order, refresh) {
  const canConfirm = order.state === 'tried' && order.suggestion && store.session.user.role === 'operator';
  return `<article class="paper order-card" id="order-${order.id}">
    <div class="blend-heading"><div><span class="mono">${esc(order.code)}</span>
      <h2>${GRADE[order.target_grade]} · ${esc(order.kilograms)} kg</h2></div>${orderStatus(order.state)}</div>
    <blockquote>${esc(order.note)}</blockquote>
    ${suggestionCard(order)}
    ${order.recipe_code ? `<p class="muted">留料草稿 <a href="#/blends/${order.recipe_id}">${esc(order.recipe_code)}</a>，配比已锁定。</p>` : ''}
    ${canConfirm ? `<form id="confirm-${order.id}" class="form-stack">
      <p class="muted">确认后按建议留足各批用量并生成配方草稿；余量不足时整单失败、一份料也不占。</p>
      ${formEnd('确认留料并生成草稿')}</form>` : ''}
    ${order.state === 'cancelled' ? '<p class="empty">草稿已撤销，锁定的留料已退回可用余量。</p>' : ''}
  </article>`;
}

function orderStatus(value) {
  const good = value === 'confirmed';
  const warn = value === 'tried';
  return `<span class="status ${good ? 'good' : warn ? 'warn' : ''}">${ORDER[value] || value}</span>`;
}

function trialForm(root, refresh) {
  root.innerHTML = `${header('定级订单试算', '填写目标等级和公斤数，从已品评、已封签的余料里找达线配比；试算只给建议，不占用余料。',
    '<a class="button quiet" href="#/orders">返回订单台账</a>')}
    <form id="order-form" class="paper form-stack order-form"><div class="two">
      <label>目标等级<select name="target_grade" required><option value="" selected disabled>选择等级</option>
        ${gradeOptions()}</select></label>
      ${field('订单公斤数', 'kilograms', '', 'type="number" min="0.01" step="0.01" placeholder="12.50"')}</div>
      ${memo('订单要求与备注')}${formEnd('开始试算')}</form>`;
  const form = root.querySelector('#order-form');
  handle(form, element => request('/orders/trial', formData(element)), () => {
    refresh('试算完成，可查看建议或失败原因。');
    location.hash = '#/orders';
  });
}

export function orders(root, id, refresh) {
  const user = store.session.user;
  if (id === 'new' && user.role === 'operator') return trialForm(root, refresh);
  const selected = store.orders.find(row => row.id === Number(id)) || store.orders[0];
  root.innerHTML = `${header('定级订单', '试算不占料，确认才留料并生成配方草稿；两人同时提交余量不足时，落后订单整笔不占料。',
    `<div class="header-actions">${user.role === 'operator' ? '<a class="button" href="#/orders/new">新建定级订单</a>' : ''}</div>`)}
    <div class="review-grid"><aside class="paper review-index"><h2>订单台账</h2><ul>${store.orders.map(row =>
      `<li><a class="${row.id === selected?.id ? 'active' : ''}" href="#/orders/${row.id}">
        <span><small>${esc(row.code)}</small><strong>${GRADE[row.target_grade]} · ${esc(row.kilograms)} kg</strong></span>
        ${orderStatus(row.state)}</a></li>`).join('') || '<li class="empty">还没有定级订单。</li>'}</ul></aside>
    ${selected ? orderCard(selected, refresh) : '<p class="empty">新建第一张定级订单。</p>'}</div>`;

  const form = root.querySelector(`#confirm-${selected?.id}`);
  if (!form) return;
  handle(form, () => request(`/orders/${selected.id}/confirm`, {}), async recipe => {
    await refresh('已留料并生成配方草稿。');
    location.hash = `#/blends/${recipe.id}`;
  });
}
