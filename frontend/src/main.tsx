import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { CommunicationDialogProvider } from './CommunicationContent'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <CommunicationDialogProvider>
      <App />
    </CommunicationDialogProvider>
  </StrictMode>,
)
