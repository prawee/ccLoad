const test = require('node:test');
const assert = require('node:assert/strict');

test('i18n defaults to English without a saved locale or English browser preference', () => {
  const names = ['window', 'navigator', 'localStorage', 'document'];
  const previous = new Map(names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const define = (name, value) => Object.defineProperty(globalThis, name, {
    configurable: true,
    enumerable: true,
    writable: true,
    value
  });
  try {
    define('window', { I18N_LOCALES: {}, dispatchEvent() {} });
    define('navigator', { language: 'zh-CN' });
    define('localStorage', { getItem() { return null; }, setItem() {} });
    define('document', { documentElement: {} });

    const modulePath = require.resolve('./i18n.js');
    delete require.cache[modulePath];
    require(modulePath);
    assert.equal(window.i18n.getLocale(), 'en');
  } finally {
    for (const name of names) {
      const descriptor = previous.get(name);
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
    delete require.cache[require.resolve('./i18n.js')];
  }
});

test('i18n interpolation preserves upstream replacement metacharacters verbatim', () => {
  const names = ['window', 'navigator', 'localStorage', 'document'];
  const previous = new Map(names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const define = (name, value) => Object.defineProperty(globalThis, name, {
    configurable: true,
    enumerable: true,
    writable: true,
    value
  });

  define('window', {
    I18N_LOCALES: {
      'zh-CN': { 'test.upstream': '第一次：{error}；第二次：{error}' }
    },
    dispatchEvent() {}
  });
  define('navigator', { language: 'zh-CN' });
  define('localStorage', { getItem() { return null; }, setItem() {} });
  define('document', { documentElement: {} });

  try {
    const modulePath = require.resolve('./i18n.js');
    delete require.cache[modulePath];
    require(modulePath);

    const upstream = "upstream says $& $` $' $$";
    assert.equal(window.t('test.upstream', { error: upstream }), `第一次：${upstream}；第二次：${upstream}`);
  } finally {
    for (const name of names) {
      const descriptor = previous.get(name);
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  }
});
