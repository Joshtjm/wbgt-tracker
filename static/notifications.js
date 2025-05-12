
async function requestNotificationPermission() {
  if (!('Notification' in window)) return;
  
  try {
    const permission = await Notification.requestPermission();
    if (permission === 'granted') {
      registerServiceWorker();
    }
  } catch (err) {
    console.error('Error requesting notification permission:', err);
  }
}

async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  
  try {
    const registration = await navigator.serviceWorker.register('/static/sw.js');
    console.log('ServiceWorker registration successful');
  } catch (err) {
    console.error('ServiceWorker registration failed:', err);
  }
}

function notifyUser(title, message) {
  if (!('Notification' in window)) return;
  
  if (Notification.permission === 'granted') {
    new Notification(title, { body: message });
  }
}

requestNotificationPermission();
