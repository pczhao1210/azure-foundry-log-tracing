import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "@fontsource-variable/noto-sans-sc";
import "./style.css";

const root = document.getElementById("root");
if (!root) throw new Error("Application root is missing.");
ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
