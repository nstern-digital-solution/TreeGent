import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from '../imports/ui/App.jsx';

import './main.css';

createRoot(document.getElementById('render-target')).render(<App />);
