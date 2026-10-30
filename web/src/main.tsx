import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { applyTheme } from './lib/auth';
import './index.css';

applyTheme((localStorage.getItem('recall.theme') as any) || 'system');

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
