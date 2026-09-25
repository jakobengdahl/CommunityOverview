import React from 'react';
import ReactDOM from 'react-dom/client';
import { I18nProvider } from './i18n';
import App from './App';
import { registerAppServiceWorker } from './pwa/registerServiceWorker';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <I18nProvider>
      <App />
    </I18nProvider>
  </React.StrictMode>
);

registerAppServiceWorker();
