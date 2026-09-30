let remote = null;
let current = null;
const error = document.querySelector('#error');

async function disconnectRemote() {
  const previous = remote;
  remote = null;
  if (!previous) return;
  await new Promise(resolve => {
    previous.addEventListener('disconnect', resolve, {once: true});
    previous.disconnect();
    setTimeout(resolve, 2000);
  });
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  if (!response.ok) throw new Error(`İşlem reddedildi (${response.status}). Oturumu ve sahipliği kontrol edin.`);
  return response.json();
}

async function refresh(reconnect = false) {
  current = await api('/api/state');
  document.querySelector('#login').hidden = true;
  document.querySelector('#computer').hidden = false;
  document.querySelector('#status').textContent = `${current.control.owner} · ${current.control.status}`;
  document.querySelector('#mode').textContent = current.control.owner === 'HUMAN' ? 'Kontrol sizde. Klavye/fare yalnız izole masaüstüne gider.' : 'Salt görüntüleme · ajan girişi lease ile korunur.';
  document.querySelector('#details').textContent = JSON.stringify(current, null, 2);
  document.querySelector('#test-input').disabled = current.control.owner !== 'AGENT' || current.control.status !== 'running';
  if (reconnect) {
    await disconnectRemote();
    document.querySelector('#screen').replaceChildren();
    if (current.runtime.running) {
      const {default: RFB} = await import('/novnc/core/rfb.js');
      remote = new RFB(document.querySelector('#screen'), `ws://${location.host}/websockify`);
      const connected = remote;
      connected.addEventListener('disconnect', () => { if (remote === connected) remote = null; });
      remote.scaleViewport = true;
      remote.resizeSession = false;
      remote.viewOnly = current.control.owner !== 'HUMAN';
      remote.addEventListener('securityfailure', () => { error.textContent = 'VNC bağlantısı reddedildi.'; });
    }
  }
}

document.querySelector('#login').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api('/api/login', {token: new FormData(event.target).get('token')});
    event.target.reset(); error.textContent = ''; await refresh(true);
  } catch (failure) { error.textContent = failure.message; }
});
for (const button of document.querySelectorAll('[data-control]')) {
  button.addEventListener('click', async () => {
    try { await disconnectRemote(); await api('/api/control', {command: button.dataset.control}); error.textContent = ''; await refresh(true); }
    catch (failure) { error.textContent = failure.message; }
  });
}
document.querySelector('#test-input').addEventListener('click', async () => {
  try {
    const queued = await api('/api/input', {lease_id: current.control.lease_id, generation: current.control.generation});
    await api(`/api/input/${queued.input_id}/execute`, {});
    error.textContent = ''; await refresh();
  } catch (failure) { error.textContent = failure.message; }
});
document.querySelector('#logout').addEventListener('click', async () => {
  try { await disconnectRemote(); await api('/api/logout', {}); location.reload(); }
  catch (failure) { error.textContent = failure.message; }
});
api('/api/session').then(result => { if (result.authenticated) return refresh(true); }).catch(() => {});
