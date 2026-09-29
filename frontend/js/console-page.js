import { createConsole } from './app.js';
import { applyStoredTheme } from './theme.js';

applyStoredTheme();
createConsole({ root: document.getElementById('app') }).start();
