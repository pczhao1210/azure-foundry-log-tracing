import { copyFileSync } from "node:fs";

copyFileSync(
  "node_modules/@fontsource-variable/noto-sans-sc/LICENSE",
  "dist/OFL-NotoSansSC.txt",
);
