/* Local-only demo cart. No checkout, cookies, analytics or personal-data forms. */
(() => {
  'use strict';
  const base = new URL('.', document.baseURI);
  const storageKey = `sloy-demo-cart:${base.pathname}`;
  const dialog = document.querySelector('.cart-dialog');
  const toast = document.querySelector('.toast');
  const formatMoney = amount => `${new Intl.NumberFormat('ru-RU').format(amount)} ₽`;
  let products = new Map();
  let cart = {};
  let ready = false;
  let toastTimer;

  function notify(message) {
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add('visible');
    toastTimer = setTimeout(() => toast.classList.remove('visible'), 2800);
  }

  function normalize(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([id, count]) =>
      products.has(id) && Number.isInteger(count) && count > 0 && count <= 99));
  }

  function readCart() {
    try { return normalize(JSON.parse(localStorage.getItem(storageKey) || '{}')); }
    catch { return {}; }
  }

  function saveCart() {
    try { localStorage.setItem(storageKey, JSON.stringify(cart)); }
    catch { /* The current page still works when browser storage is unavailable. */ }
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function action(label, text, handler, className = '') {
    const button = element('button', className, text);
    button.type = 'button';
    button.setAttribute('aria-label', label);
    button.addEventListener('click', handler);
    return button;
  }

  function emptyCart() {
    const block = element('div', 'cart-empty');
    block.append(element('span', 'empty-symbol', '✳'), element('h3', '', 'Место для любимого'));
    block.append(element('p', '', 'Пока здесь тихо. Выберите предмет, с которого начнётся ваша подборка.'));
    const link = element('a', '', 'Посмотреть коллекцию ↗');
    link.href = 'catalog.html';
    block.append(link);
    return block;
  }

  function updateQuantity(id, change, focusAction) {
    const next = (cart[id] || 0) + change;
    if (next <= 0) delete cart[id];
    else cart[id] = Math.min(next, 99);
    saveCart();
    render();
    // Re-rendering a quantity control must not send keyboard focus to the body.
    if (focusAction) {
      const container = dialog.open ? dialog : document.querySelector('.cart-page-items');
      const replacement = container?.querySelector(`[data-item="${id}"] [data-action="${focusAction}"]:not(:disabled)`);
      (replacement || container?.querySelector('button, a') || dialog.querySelector('[data-close-cart]')).focus();
    }
  }

  function cartLine(product, count) {
    const row = element('article', 'cart-line');
    row.dataset.item = product.id;
    const imageLink = element('a', product.color);
    imageLink.href = `${product.id}.html`;
    imageLink.setAttribute('aria-label', `${product.type} ${product.name}`);
    const image = element('img');
    image.src = `assets/${product.id}.svg`;
    image.alt = '';
    image.width = 115;
    image.height = 123;
    imageLink.append(image);
    const content = element('div');
    const heading = element('div', 'cart-line-head');
    const name = element('div');
    const title = element('h3');
    const link = element('a', '', product.name);
    link.href = `${product.id}.html`;
    title.append(link);
    name.append(title, element('small', '', product.type));
    const remove = action(`Убрать ${product.name} из корзины`, '×', () => {
      const removedCount = cart[product.id];
      updateQuantity(product.id, -removedCount, 'minus');
      notify(`${product.name}: удалено из подборки`);
    }, 'remove-item');
    heading.append(name, remove);
    const bottom = element('div', 'cart-line-bottom');
    const quantity = element('div', 'quantity');
    quantity.setAttribute('role', 'group');
    quantity.setAttribute('aria-label', `Количество: ${product.name}`);
    const minus = action(`Уменьшить количество ${product.name}`, '−', () => updateQuantity(product.id, -1, 'minus'));
    minus.dataset.action = 'minus';
    const number = element('span', '', String(count));
    number.setAttribute('aria-label', `${count} шт.`);
    const plus = action(`Увеличить количество ${product.name}`, '+', () => updateQuantity(product.id, 1, 'plus'));
    plus.dataset.action = 'plus';
    plus.disabled = count >= 99;
    quantity.append(minus, number, plus);
    bottom.append(quantity, element('span', 'line-price', formatMoney(product.price * count)));
    content.append(heading, bottom);
    row.append(imageLink, content);
    return row;
  }

  function render() {
    const entries = Object.entries(cart);
    const count = entries.reduce((sum, [, quantity]) => sum + quantity, 0);
    const total = entries.reduce((sum, [id, quantity]) => sum + products.get(id).price * quantity, 0);
    document.querySelectorAll('[data-cart-count]').forEach(node => { node.textContent = String(count); });
    document.querySelectorAll('[data-cart-total]').forEach(node => { node.textContent = formatMoney(total); });
    document.querySelectorAll('[data-cart-items]').forEach(node => {
      node.replaceChildren(...(entries.length ? entries.map(([id, quantity]) => cartLine(products.get(id), quantity)) : [emptyCart()]));
    });
    document.querySelectorAll('[data-clear-cart]').forEach(node => { node.disabled = count === 0; });
  }

  document.addEventListener('click', event => {
    const add = event.target.closest('[data-add]');
    if (add) {
      if (!ready) { notify('Корзина загружается. Попробуйте ещё раз.'); return; }
      const id = add.dataset.add;
      if (!products.has(id)) return;
      if (cart[id] >= 99) { notify('В учебной корзине доступно до 99 одинаковых предметов.'); return; }
      updateQuantity(id, 1);
      dialog.showModal();
      return;
    }
    if (event.target.closest('[data-open-cart]')) {
      if (!ready) { notify('Корзина загружается. Попробуйте ещё раз.'); return; }
      dialog.showModal();
    }
    if (event.target.closest('[data-close-cart]')) dialog.close();
    if (event.target.closest('[data-clear-cart]')) {
      cart = {};
      saveCart();
      render();
      document.querySelector('.cart-page-items a')?.focus();
      notify('Подборка очищена');
    }
  });

  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });

  window.addEventListener('storage', event => {
    if (ready && (event.key === storageKey || event.key === null)) { cart = readCart(); render(); }
  });

  fetch(new URL('assets/products.json', base)).then(response => {
    if (!response.ok) throw new Error('Catalog unavailable');
    return response.json();
  }).then(catalog => {
    products = new Map(catalog.map(product => [product.id, product]));
    cart = readCart();
    ready = true;
    render();
  }).catch(() => {
    document.querySelectorAll('[data-add], [data-open-cart], [data-clear-cart]').forEach(button => { button.disabled = true; });
    document.querySelectorAll('[data-cart-items]').forEach(node => {
      const block = element('div', 'cart-empty');
      block.append(element('h3', '', 'Не удалось загрузить корзину'), element('p', '', 'Обновите страницу. Для локального просмотра откройте витрину через HTTP-сервер.'));
      node.replaceChildren(block);
    });
    notify('Корзина временно недоступна. Обновите страницу.');
  });
})();
