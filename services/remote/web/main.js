// The page's entry: which door served it (door.json: a static "relay" on the relay; QiTV itself
// answers "home"), then each part registers with app.js, then the page starts.
import { start } from "./app.js";
import { install } from "./relay.js";
import { install as installScreens } from "./screens.js";

let door = "home";
try {
  const response = await fetch("door.json", { cache: "no-store" });
  if (response.ok && (await response.json()).door === "relay") door = "relay";
} catch {
  // an unreadable answer: the home door, which works on the home network
}
if (!(await install(door))) {
  installScreens();
  start();
}
