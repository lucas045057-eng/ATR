export type Theme = 'light' | 'dark';

export function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem('atr_theme');
    if (saved === 'light' || saved === 'dark') return saved;
  } catch { /* Browser storage may be disabled. */ }
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  try { localStorage.setItem('atr_theme', theme); } catch { /* Theme still works for this page. */ }
}
