// Initialize socket connection
const socket = io();

// Make sure currentUser is defined globally
if (typeof currentUser === 'undefined') {
  var currentUser = null;
}

if (typeof userRole === 'undefined') {
  var userRole = null;
}

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
    new Notification(title, { 
      body: message,
      icon: '/static/icon.png'
    });
  }
}

socket.on('work_complete', (data) => {
  // Only handle events if currentUser is defined
  if (currentUser && data.user === currentUser) {
    if (confirm('Work cycle complete. Start rest period now?')) {
      fetch('/start_rest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `username=${currentUser}`
      });
    }
  }
});

socket.on('rest_reminder', (data) => {
  // Only handle events if userRole is defined
  if (userRole && (userRole === 'Safety Officer' || userRole === 'Supervisor')) {
    notifyUser('Rest Period Warning', data.message);
  }
});

requestNotificationPermission();