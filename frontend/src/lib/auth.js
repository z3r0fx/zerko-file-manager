import { apiCall } from './api.js';

function login(username, password) {
  return apiCall('/api/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  }).then((data) => {
    if (data.token) {
      localStorage.setItem('token', data.token);
    }
    return data;
  });
}

function logout() {
  localStorage.removeItem('token');
}

function getToken() {
  return localStorage.getItem('token');
}

export { login, logout, getToken };