import React from 'react';
import { createRoot } from 'react-dom/client';
import { Status } from './status.jsx';

createRoot(document.getElementById('root')).render(<Status message="Offline build proof" />);
