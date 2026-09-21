import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './index.css';
import { AuthProvider } from './context/AuthContext';
import { ThemeProvider } from './context/ThemeContext';
import { AppearanceProvider } from './context/AppearanceContext';
import { DataProvider } from './context/DataContext';
import PointerEffects from './components/shared/PointerEffects';
import LongPress from './components/shared/LongPress';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <AppearanceProvider>
      <PointerEffects />
      <LongPress />
      <ThemeProvider>
        <AuthProvider>
          <DataProvider>
            <App />
          </DataProvider>
        </AuthProvider>
      </ThemeProvider>
    </AppearanceProvider>
  </React.StrictMode>
);