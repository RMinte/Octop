import { createContext, useContext } from "react";
export const LegacyHistoryContext = createContext(false);
export const useLegacyHistoryReadOnly = () => useContext(LegacyHistoryContext);
