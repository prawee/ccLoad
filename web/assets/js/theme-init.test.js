const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'theme-init.js'), 'utf8');

function memoryStorage() {
  const values = new Map();
  return {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, String(value)); }
  };
}

function cookieJar() {
  const values = new Map();
  return {
    get value() {
      return [...values].map(([key, value]) => `${key}=${value}`).join('; ');
    },
    set value(raw) {
      const [pair] = String(raw).split(';');
      const separator = pair.indexOf('=');
      values.set(pair.slice(0, separator), pair.slice(separator + 1));
    }
  };
}

function loadTheme(storage, cookies, prefersDark = false) {
  const style = {
    colorScheme: '',
    backgroundColor: '',
    color: '',
    removeProperty() {}
  };
  const document = {
    documentElement: { dataset: {}, style },
    readyState: 'loading',
    querySelector() { return null; },
    addEventListener() {},
    get cookie() { return cookies.value; },
    set cookie(value) { cookies.value = value; }
  };
  const listeners = [];
  const systemQuery = {
    matches: prefersDark,
    addEventListener(type, listener) {
      if (type === 'change') listeners.push(listener);
    }
  };
  const window = {
    matchMedia() { return systemQuery; }
  };

  vm.runInNewContext(source, {
    window,
    document,
    localStorage: storage,
    requestAnimationFrame() {}
  });
  return {
    window,
    document,
    changeSystemTheme(prefersDark) {
      systemQuery.matches = prefersDark;
      listeners.forEach(listener => listener());
    }
  };
}

test('no saved preference defaults to light regardless of system theme', () => {
  for (const prefersDark of [false, true]) {
    const storage = memoryStorage();
    const cookies = cookieJar();
    const page = loadTheme(storage, cookies, prefersDark);
    const root = page.document.documentElement;

    assert.equal(page.window.ccLoadTheme.getStoredTheme(), 'light');
    assert.equal(root.dataset.theme, 'light');
    assert.equal(root.dataset.resolvedTheme, 'light');
    assert.equal(root.style.colorScheme, 'light');
    assert.equal(root.style.backgroundColor, '#fcfbf9');
    assert.equal(root.style.color, '#111827');
    page.changeSystemTheme(!prefersDark);
    assert.equal(root.dataset.resolvedTheme, 'light');
    assert.equal(storage.getItem('ccload_theme'), null);
    assert.equal(cookies.value, '');
  }
});

test('saved modes override the default in either storage format and location', () => {
  for (const mode of ['light', 'dark', 'system']) {
    for (const value of [mode, `${mode}:123`]) {
      for (const location of ['local', 'cookie']) {
        for (const prefersDark of [false, true]) {
          const storage = memoryStorage();
          const cookies = cookieJar();
          if (location === 'local') storage.setItem('ccload_theme', value);
          else cookies.value = `ccload_theme=${encodeURIComponent(value)}`;
          const page = loadTheme(storage, cookies, prefersDark);
          const root = page.document.documentElement;
          const resolved = mode === 'system' ? (prefersDark ? 'dark' : 'light') : mode;

          assert.equal(root.dataset.theme, mode);
          assert.equal(root.dataset.resolvedTheme, resolved);
          assert.equal(root.style.colorScheme, resolved);
          page.changeSystemTheme(!prefersDark);
          assert.equal(root.dataset.theme, mode);
          assert.equal(root.dataset.resolvedTheme, mode === 'system' ? (prefersDark ? 'light' : 'dark') : mode);
        }
      }
    }
  }
});

test('missing or invalid preferences default to light when storage is blocked', () => {
  for (const value of [null, 'invalid']) {
    const storage = {
      getItem() { if (value === null) throw new Error('storage blocked'); return value; }
    };
    const cookies = {
      get value() { if (value === null) throw new Error('cookies blocked'); return 'ccload_theme=invalid'; }
    };
    const page = loadTheme(storage, cookies, true);
    assert.equal(page.document.documentElement.dataset.theme, 'light');
    assert.equal(page.document.documentElement.dataset.resolvedTheme, 'light');
  }
});

test('admin fallback defaults to light and preserves saved modes', () => {
  const uiSource = fs.readFileSync(path.join(__dirname, 'ui.js'), 'utf8');
  const start = uiSource.indexOf('  function getStoredTheme()');
  const end = uiSource.indexOf('  function resolveTheme(mode)', start);
  assert.ok(start >= 0 && end > start);
  const fallback = uiSource.slice(start, end);

  for (const saved of [null, 'invalid', 'light', 'dark', 'system', 'dark:123', 'system:123']) {
    const mode = saved?.split(':')[0];
    const expected = ['light', 'dark', 'system'].includes(mode) ? mode : 'light';
    const actual = vm.runInNewContext(`${fallback}\ngetStoredTheme();`, {
      window: {},
      localStorage: { getItem() { return saved; } },
      THEME_STORAGE_KEY: 'ccload_theme',
      THEME_MODES: ['system', 'light', 'dark']
    });
    assert.equal(actual, expected);
  }
  assert.equal(vm.runInNewContext(`${fallback}\ngetStoredTheme();`, {
    window: {},
    localStorage: { getItem() { throw new Error('storage blocked'); } }
  }), 'light');
});

test('explicit theme survives a page reload', () => {
  const storage = memoryStorage();
  const cookies = cookieJar();
  const firstPage = loadTheme(storage, cookies);

  assert.equal(firstPage.window.ccLoadTheme.setStoredTheme('dark'), true);

  const reloadedPage = loadTheme(storage, cookies);
  assert.equal(reloadedPage.document.documentElement.dataset.theme, 'dark');
  assert.equal(reloadedPage.document.documentElement.dataset.resolvedTheme, 'dark');
});

test('cookie preserves the theme when Edge-style browser storage is unavailable', () => {
  const blockedStorage = {
    getItem() { throw new Error('storage blocked'); },
    setItem() { throw new Error('storage blocked'); }
  };
  const cookies = cookieJar();
  const firstPage = loadTheme(blockedStorage, cookies);

  assert.equal(firstPage.window.ccLoadTheme.setStoredTheme('dark'), true);

  const reloadedPage = loadTheme(blockedStorage, cookies);
  assert.equal(reloadedPage.document.documentElement.dataset.theme, 'dark');
  assert.equal(reloadedPage.document.documentElement.dataset.resolvedTheme, 'dark');
});

test('local storage preserves the newest theme when the cookie is readable but not writable', () => {
  const storage = memoryStorage();
  const cookies = {
    get value() { return 'ccload_theme=dark'; },
    set value(_) {}
  };
  const firstPage = loadTheme(storage, cookies);

  assert.equal(firstPage.document.documentElement.dataset.theme, 'dark');
  assert.equal(firstPage.window.ccLoadTheme.setStoredTheme('light'), true);

  const reloadedPage = loadTheme(storage, cookies);
  assert.equal(reloadedPage.document.documentElement.dataset.theme, 'light');
  assert.equal(reloadedPage.document.documentElement.dataset.resolvedTheme, 'light');
});

test('cookie preserves the newest theme when local storage is readable but not writable', () => {
  const storage = {
    getItem() { return 'light'; },
    setItem() { throw new Error('storage is read-only'); }
  };
  const cookies = cookieJar();
  const firstPage = loadTheme(storage, cookies);

  assert.equal(firstPage.document.documentElement.dataset.theme, 'light');
  assert.equal(firstPage.window.ccLoadTheme.setStoredTheme('dark'), true);

  const reloadedPage = loadTheme(storage, cookies);
  assert.equal(reloadedPage.document.documentElement.dataset.theme, 'dark');
  assert.equal(reloadedPage.document.documentElement.dataset.resolvedTheme, 'dark');
});
