const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'ui.js'), 'utf8');
const logoURL = '/web/brand-wordmark-mittr.svg?v=__VERSION__';
const faviconURL = '/web/favicon-mittr.ico';

test('all admin pages use only the supplied ICO as their browser favicon', () => {
  const webRoot = path.join(__dirname, '../..');
  assert.ok(fs.statSync(path.join(webRoot, 'favicon-mittr.ico')).size > 0);
  const pages = fs.readdirSync(webRoot).filter(file => file.endsWith('.html'));
  assert.ok(pages.length > 0);
  for (const page of pages) {
    const html = fs.readFileSync(path.join(webRoot, page), 'utf8');
    const links = (html.match(/<link\b[^>]*>/g) || []).filter(link => /\brel="(?:shortcut )?icon"/.test(link));
    assert.equal(links.length, 1, page);
    assert.ok(links[0].includes(`href="${faviconURL}"`), page);
    assert.match(links[0], /type="image\/x-icon"/, page);
    assert.doesNotMatch(html, /\/web\/favicon\.(?:svg|ico)/, page);
  }
});

test('activity favicon draws badges over the supplied ICO and restores the ICO fallback', () => {
  const start = source.indexOf('  function faviconBadgeLabel(count) {');
  const end = source.indexOf('  function redrawActiveFavicon()', start);
  const replacements = [];
  const images = [];
  const labels = [];
  const radii = [];
  const context = {
    Image: class {
      set src(value) { this.url = value; this.onload(); }
    },
    document: {
      querySelectorAll: () => [],
      createElement(tag) {
        assert.equal(tag, 'canvas');
        return {
          getContext: () => ({
            clearRect() {},
            drawImage(image) { images.push(image.url); },
            beginPath() {},
            arc(x, y, radius) { radii.push(radius); },
            fill() {},
            fillText(text) { labels.push(text); }
          }),
          toDataURL(type) {
            assert.equal(type, 'image/png');
            return 'data:image/png;base64,badge';
          }
        };
      }
    },
    recordReplacement(descriptors, dynamic) {
      replacements.push({ descriptors: JSON.parse(JSON.stringify(descriptors)), dynamic });
    }
  };
  vm.runInNewContext(`let _faviconBase = null; let _origFaviconLinks = null;
${source.slice(start, end)}
replaceFaviconSet = recordReplacement;
ensureFaviconBase(() => drawFaviconBadge(12, true));
ensureFaviconBase(() => drawFaviconBadge(3, false));
restoreFavicon();`, context);
  assert.deepEqual(images, [faviconURL, faviconURL]);
  assert.deepEqual(labels, ['9+', '3']);
  assert.deepEqual(radii, [18, 15, 13, 15, 13, 11]);
  for (const replacement of replacements.slice(0, 2)) {
    assert.equal(replacement.dynamic, true);
    assert.deepEqual(replacement.descriptors, ['shortcut icon', 'icon'].map(rel => ({
      rel, href: 'data:image/png;base64,badge', type: 'image/png', sizes: ''
    })));
  }
  assert.deepEqual(replacements[2], {
    descriptors: [{ rel: 'icon', href: faviconURL, type: 'image/x-icon', sizes: '' }],
    dynamic: false
  });
  assert.doesNotMatch(source, /\/web\/favicon\.(?:svg|ico)/);
});

function renderTopbar() {
  const start = source.indexOf('  function buildTopbar(active) {');
  const end = source.indexOf('  function buildThemeSwitcher()', start);
  const context = {
    h(tag, attrs = {}, children = []) {
      return {
        tag, attrs,
        children: Array.isArray(children) ? children : [children],
        appendChild(child) { this.children.push(child); }
      };
    },
    window: { getWebRole: () => 'admin', WebAuth: { filterNavigation: () => [] } },
    NAVS: [],
    BRAND_FLOW_DOTS_MAX: 5,
    GITHUB_REPO_URL: '',
    GITHUB_RELEASES_URL: '',
    t: key => key,
    isLoggedIn: () => true,
    onLogout() {},
    iconGitHub: () => null,
    buildThemeSwitcher: () => null
  };
  return vm.runInNewContext(`${source.slice(start, end)}\nbuildTopbar('index');`, context);
}

function descendants(node) {
  if (!node || typeof node !== 'object') return [];
  return [node, ...node.children.flatMap(descendants)];
}

test('topbar renders the mittr wordmark without a separate logo and retains activity indicators', () => {
  const nodes = descendants(renderTopbar());
  const brand = nodes.find(node => node.attrs.class === 'brand');
  assert.equal(brand.attrs['aria-label'], 'mittr load');
  assert.equal(brand.children.length, 1);
  const wrap = brand.children[0];
  assert.equal(wrap.attrs.class, 'brand-icon-wrap');
  const images = descendants(brand).filter(node => node.tag === 'img');
  assert.equal(images.length, 1);
  assert.equal(images[0].attrs.src, logoURL);
  assert.equal(images[0].attrs.alt, 'mittr load');
  assert.equal(images[0].attrs.width, 132);
  assert.equal(images[0].attrs.height, 36);
  for (const className of ['brand-speed-lines', 'brand-flow-dots', 'brand-badge']) {
    assert.ok(wrap.children.some(node => node.attrs.class === className));
  }
  assert.ok(!descendants(brand).some(node => node.tag === 'svg' || node.attrs.class === 'brand-wordmark'));
  assert.doesNotMatch(source, /BRAND_MARK_SVG|BRAND_WORDMARK_SVG|createInlineBrandSVG/);
});

test('login renders one full mittr logo in the existing wrapper instead of the favicon and wordmark pair', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../login.html'), 'utf8');
  const brand = html.slice(html.indexOf('<div class="login-brand">'), html.indexOf('<div class="login-form-container">'));
  assert.match(brand, /class="login-brand-row"/);
  assert.match(brand, /class="brand-logo"/);
  const images = brand.match(/<img\b[^>]*>/g);
  assert.equal(images.length, 1);
  assert.ok(images[0].includes(`src="${logoURL}"`));
  assert.match(images[0], /alt="mittr load"/);
  assert.match(images[0], /width="176" height="48"/);
  assert.doesNotMatch(brand, /<svg|<use|favicon|class="login-brand-wordmark"|brand-mark-mittr/);
});
