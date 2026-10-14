import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { bootstrapSessionFromUrl } from "./session";
import "./index.css";

// Adopt a key/server handed in the URL (the Electron shell passes ?key=) before the first render,
// so the app opens already authenticated instead of on the key prompt.
bootstrapSessionFromUrl();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
