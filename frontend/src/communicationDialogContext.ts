import { createContext, useContext } from 'react'

import type { CommunicationDialogTarget } from './communicationDialog'


export type CommunicationDialogApi = {
  openCommunication: (target: CommunicationDialogTarget) => void
  closeCommunication: () => void
}

export const CommunicationDialogContext = createContext<CommunicationDialogApi>({
  openCommunication: () => undefined,
  closeCommunication: () => undefined,
})


export function useCommunicationDialog() {
  return useContext(CommunicationDialogContext)
}
