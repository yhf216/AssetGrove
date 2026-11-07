import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import './layout.css'
import './usability.css'
import './theme.css'
import './market.css'
import { installWheelGuard } from './wheel'
installWheelGuard()
createApp(App).mount('#app')
