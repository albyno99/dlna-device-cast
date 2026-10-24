# Local receiver add-ons

Drop `.js` files here. The sender appends them to the receiver code it serves, so they run on the TV
during a cast. This folder is ignored by git, apart from this file.

## How add-ons are loaded

- Every `*.js` file in this folder is loaded in name order (`10-clock.js` before `20-alerts.js`).
  Symbolic links are followed, so an add-on can live in another project.
- Files are read again each time the TV loads the receiver: restart the cast to pick up changes.
- Each add-on is compiled and run on its own inside `try`/`catch`. A syntax or runtime error is logged
  to the TV's console and does not stop the receiver or the other add-ons. Errors in callbacks that run
  later (timers, events, network requests) are not caught: handle them in the add-on.
- Files larger than 512 KB are skipped. The cast log lists the add-ons loaded.

## Environment

Add-ons run in the page's global scope, after the receiver has started. The receiver's own variables
are not visible to them.

- The TV browser is Chromium 56 on Samsung Tizen 4: use ES5 or features available in Chrome 56.
- Add your own elements to `document.body` with `position: fixed` and a `z-index` above 5. The receiver
  uses these layers:

  | z-index | Layer |
  |---|---|
  | (none) | tile canvas |
  | 3 | video (H.264 mode) |
  | 4 | cursor |
  | 5 | status message (`#msg`) |

- The Tizen app is allowed to reach the internet, so `fetch`, `XMLHttpRequest` and `EventSource`
  can be used.
- Listen to `keydown` on `document`. The receiver does not stop the propagation of any key. These keys
  already have a meaning:

  | Key | keyCode | Receiver action |
  |---|---|---|
  | Return | 10009 | exits the app and ends the cast |
  | Info | 10073 | shows or hides the stats overlay |
  | Green | 404 | shows or hides the stats overlay |
  | `i` (keyboard) | | shows or hides the stats overlay |

  Other keys, such as OK (13) and the arrows (37 to 40), are free for add-ons.

## Example

```js
// receiver/addons/hello.js: shows a label for five seconds when OK is pressed
var box = document.createElement('div');
box.style.cssText = 'position:fixed;right:40px;top:40px;z-index:10;padding:12px 18px;' +
  'background:rgba(0,0,0,.7);color:#fff;font:24px sans-serif;border-radius:8px;display:none';
box.textContent = 'Hello from an add-on';
document.body.appendChild(box);
document.addEventListener('keydown', function (e) {
  if (e.keyCode !== 13) return;
  box.style.display = 'block';
  setTimeout(function () { box.style.display = 'none'; }, 5000);
});
```
