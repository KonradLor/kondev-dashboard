// app.js - Alpine.js-powered dashboard logic (minimal, no build step)

function rand(min, max){ return Math.random() * (max - min) + min; }
function clamp(v, a, b){ return Math.max(a, Math.min(b, v)); }

const sampleServices = [
  { name: "konradvault", display_name: "KonradVault", description: "Asmeninė failų saugykla", icon: "📁", category: "storage", status: "running", url: "/vault/", estimated_ram_mb: 384, estimated_cpu_percent: 1.5 },
  { name: "minecraft", display_name: "Minecraft Server", description: "Paper 1.19.1 + BlueMap", icon: "⛏️", category: "game", status: "stopped", url: null, estimated_ram_mb: 6144, estimated_cpu_percent: 25 },
  { name: "beszel", display_name: "Beszel Monitor", description: "Serverio resursų stebėjimas", icon: "📊", category: "monitor", status: "running", url: "/monitor/", estimated_ram_mb: 80, estimated_cpu_percent: 0.5 },
  { name: "nextcloud", display_name: "Nextcloud", description: "Failų sinchronizacija ir bendrinimas", icon: "☁️", category: "sync", status: "stopped", url: null, estimated_ram_mb: 512, estimated_cpu_percent: 3 },
  { name: "gitea", display_name: "Gitea", description: "Asmeninis Git serveris", icon: "🌿", category: "git", status: "stopped", url: null, estimated_ram_mb: 256, estimated_cpu_percent: 1 },
  { name: "vaultwarden", display_name: "Vaultwarden", description: "Slaptažodžių saugykla", icon: "🔐", category: "auth", status: "running", url: "/vault-pw/", estimated_ram_mb: 128, estimated_cpu_percent: 0.2 }
];

const sampleEvents = [
  { id: 1, type: 'service_started', service: 'konradvault', message: 'konradvault paleistas', timestamp: new Date(Date.now() - 5*60*1000).toISOString() },
  { id: 2, type: 'service_started', service: 'beszel', message: 'beszel paleistas', timestamp: new Date(Date.now() - 20*60*1000).toISOString() },
  { id: 3, type: 'service_sleep', service: 'minecraft', message: 'minecraft užmigo', timestamp: new Date(Date.now() - 60*60*1000).toISOString() },
  { id: 4, type: 'service_error', service: 'nextcloud', message: 'nextcloud klaida: prisijungimas', timestamp: new Date(Date.now() - 2*60*60*1000).toISOString() }
];

const sampleResources = {
  cpu_breakdown: [
    { service: 'caddy', percent: 0.5, color: '#f59e0b' },
    { service: 'beszel', percent: 2.1, color: '#8b5cf6' },
    { service: 'free', percent: 97.4, color: '#27272a' }
  ],
  ram_breakdown: [
    { service: 'konradvault', percent: 2.0, color: '#f59e0b' },
    { service: 'minecraft', percent: 25.0, color: '#fb923c' },
    { service: 'beszel', percent: 0.3, color: '#8b5cf6' },
    { service: 'free', percent: 72.7, color: '#27272a' }
  ]
};

function dashboard(){
  return {
    polling: true,
    isOffline: false,
    stats: { cpu_percent: 23.5, ram_used_gib: 4.2, ram_total_gib: 23.0, ram_percent: 18.3, disk_used_gb: 4.1, disk_total_gb: 200.0, disk_percent: 2.1, updated_at: new Date().toISOString() },
    serverInfo: { hostname: 'server-main-max', public_ip: '130.61.182.31', os: 'Ubuntu 24.04 LTS', arch: 'aarch64', cpu_model: 'ARM Neoverse-N1', cpu_cores: 4, ram_gib: 23, uptime_seconds: 172800 },
    services: JSON.parse(JSON.stringify(sampleServices)),
    events: JSON.parse(JSON.stringify(sampleEvents)),
    resources: JSON.parse(JSON.stringify(sampleResources)),
    unmanagedDockers: [],     // veikiantys konteineriai, kurių nėra services.yaml (admin)

    // Vartotojų valdymas (FAZĖ 4 - Authentik)
    users: [],
    usersError: '',
    usersMsg: '',

    // Autentifikacijos būsena (centrinis OIDC per Authentik)
    authenticated: false,     // true = prisijungęs (vartotojas ar admin)
    currentUser: null,        // rodomas vardas
    isAdmin: false,           // true tik administratoriui (Authentik 'authentik Admins' grupė)
    showLoginForm: false,
    loginUsername: '',
    loginPassword: '',
    loginError: '',

    statsInterval: null, servicesInterval: null, resourcesInterval: null,

    async init(){
      // PIRMAS žingsnis - sužinom, ar jau prisijungęs (cookie iš ankstesnės sesijos)
      await this.fetchMe();
      this.fetchAll();
      this.statsInterval = setInterval(()=>this.fetchStats(), 5000);
      this.servicesInterval = setInterval(()=>{ this.fetchServices(); this.fetchUnmanaged(); }, 10000);
      this.resourcesInterval = setInterval(()=>this.fetchResources(), 10000);
      // Lucide ikonų atnaujinimas - kas 2s pertikrinam DOM, ar yra naujų ikonų
      // (kai Alpine re-render'ina template'ą, <i> tag'ai grįžta - reikia konvertuoti į SVG)
      setInterval(() => this.renderIcons(), 2000);
      // Pirmas rendering
      setTimeout(() => this.renderIcons(), 100);
    },

    async fetchAll(){
      this.fetchStats();
      this.fetchServices();
      this.fetchResources();
      // Admin-only endpoints
      if(this.isAdmin){
        this.fetchServerInfo();
        this.fetchEvents();
        this.fetchUnmanaged();
        this.loadUsers();
      }
    },

    // ---- Vartotojų valdymas (admin, per Authentik API) ----
    async loadUsers(){
      if(!this.isAdmin) return;
      this.usersError = '';
      try{
        let res = await fetch('/api/admin/users');
        if(!res.ok) throw new Error('status ' + res.status);
        let data = await res.json();
        this.users = data.users || [];
      }catch(e){
        this.usersError = 'Nepavyko gauti vartotojų sąrašo';
      }
    },

    _flashUsersMsg(msg){
      this.usersMsg = msg;
      this.usersError = '';
      setTimeout(()=>{ if(this.usersMsg === msg) this.usersMsg=''; }, 6000);
    },

    async _userAction(u, path, body, okMsg){
      this.usersError = '';
      try{
        let res = await fetch('/api/admin/users/' + u.pk + path, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body || {})
        });
        let data = await res.json().catch(()=>({}));
        if(!res.ok){ this.usersError = data.detail || ('Klaida (' + res.status + ')'); return false; }
        this._flashUsersMsg(data.message || okMsg);
        return true;
      }catch(e){ this.usersError = 'Tinklo klaida'; return false; }
    },

    async userResetPassword(u){
      if(!confirm('Siųsti slaptažodžio atstatymo laišką vartotojui ' + u.name + ' (' + u.email + ')?')) return;
      await this._userAction(u, '/reset-password', {}, 'Laiškas išsiųstas');
    },

    async userResendVerification(u){
      if(!confirm('Siųsti patvirtinimo laišką vartotojui ' + u.name + ' (' + u.email + ')?')) return;
      await this._userAction(u, '/resend-verification', {}, 'Laiškas išsiųstas');
    },

    async userChangeEmail(u){
      let v = prompt('Naujas email adresas vartotojui ' + u.name + ':', u.email || '');
      if(v === null) return;
      v = v.trim();
      if(!v || v === u.email) return;
      let ok = await this._userAction(u, '/email', {email: v}, 'Email pakeistas');
      if(ok) u.email = v;
    },

    async userToggleActive(u){
      let veiksmas = u.is_active ? 'išjungti (deaktyvuoti)' : 'aktyvuoti';
      if(!confirm('Ar tikrai ' + veiksmas + ' vartotoją ' + u.name + '?')) return;
      let ok = await this._userAction(u, '/active', {is_active: !u.is_active}, 'Atnaujinta');
      if(ok) u.is_active = !u.is_active;
    },

    async userDelete(u){
      // Pirmas patvirtinimas - aiškus įspėjimas apie negrįžtamumą
      if(!confirm('⚠️ NEGRĮŽTAMAI ištrinti vartotoją ' + u.name + ' (' + u.email + ')?\n\n'
        + 'Bus ištrinta VISKAS:\n'
        + '• visi jo failai vault\'e (iš disko)\n'
        + '• visi duomenų bazės įrašai\n'
        + '• tapatybė (login) Authentik\'e\n\n'
        + 'Šio veiksmo ATŠAUKTI NEGALIMA.')) return;
      // Antras (papildomas) patvirtinimas
      if(!confirm('Ar tikrai norite ištrinti ' + u.name + '? Paskutinis patvirtinimas.')) return;
      this.usersError = '';
      try{
        let res = await fetch('/api/admin/users/' + u.pk, { method: 'DELETE' });
        let data = await res.json().catch(()=>({}));
        if(!res.ok){ this.usersError = data.detail || ('Klaida (' + res.status + ')'); return; }
        this._flashUsersMsg(data.message || 'Vartotojas ištrintas');
        // Pašalinam iš sąrašo iškart
        this.users = this.users.filter(x => x.pk !== u.pk);
      }catch(e){ this.usersError = 'Tinklo klaida'; }
    },

    async fetchUnmanaged(){
      if(!this.isAdmin) return;
      try{
        let res = await fetch('/api/docker/unmanaged');
        if(!res.ok) return;
        let data = await res.json();
        this.unmanagedDockers = data.containers || [];
      }catch(e){ /* tylim - nebūtina */ }
    },

    async fetchMe(){
      try{
        let res = await fetch('/api/me');
        if(!res.ok) throw new Error('no');
        let data = await res.json();
        this.authenticated = !!data.authenticated;
        this.currentUser = data.user;
        this.isAdmin = !!data.is_admin;
      }catch(e){
        this.authenticated = false;
        this.currentUser = null;
        this.isAdmin = false;
      }
    },

    // Centrinis prisijungimas / registracija (Authentik)
    goLogin(){ window.location.href = '/auth/login'; },
    goRegister(){ window.location.href = 'https://auth.kondev.app/if/flow/enroll/'; },
    // 2FA valdymas - Authentik vartotojo nustatymai (MFA Devices). Naujame skirtuke,
    // kad nedingtų dashboard. Veikia visiems prisijungusiems (neprivaloma įsijungti).
    goSecurity(){ window.open('https://auth.kondev.app/if/user/#/settings', '_blank', 'noopener'); },

    async login(){
      this.loginError = '';
      try{
        let res = await fetch('/api/login', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({username: this.loginUsername, password: this.loginPassword})
        });
        if(res.status === 401){
          this.loginError = 'Neteisingas vartotojas arba slaptažodis';
          return;
        }
        if(!res.ok){
          this.loginError = 'Klaida: ' + res.status;
          return;
        }
        let data = await res.json();
        this.currentUser = data.user;
        this.isAdmin = true;
        this.showLoginForm = false;
        this.loginUsername = '';
        this.loginPassword = '';
        // Perkraunam visus duomenis - admin matys daugiau
        this.fetchAll();
      }catch(e){
        this.loginError = 'Tinklo klaida';
      }
    },

    async logout(){
      // OIDC: /auth/logout išvalo dashboard sesiją ir grąžina į kondev.app.
      // (Authentik SSO sesija lieka - kitas prisijungimas vienu paspaudimu.)
      window.location.href = '/auth/logout';
    },

    async fetchStats(){
      try{
        let res = await fetch('/api/stats');
        if(!res.ok) throw new Error('no');
        let data = await res.json();
        Object.assign(this.stats, data);
        this.isOffline = false;
      }catch(e){
        this.isOffline = true;
        this.mockStatsUpdate();
      }
    },

    async fetchServerInfo(){
      try{
        let res = await fetch('/api/server-info');
        if(res.status === 401){
          // Sesija pasibaigė - atsijungiam tylėdami
          this.currentUser = null;
          this.isAdmin = false;
          return;
        }
        if(!res.ok) throw new Error('no');
        let data = await res.json();
        Object.assign(this.serverInfo, data);
        this.isOffline = false;
      }catch(e){
        this.isOffline = true;
      }
    },

    async fetchServices(){
      try{
        let res = await fetch('/api/services');
        if(!res.ok) throw new Error('no');
        let data = await res.json();
        this.services = data.services;
        this.isOffline = false;
      }catch(e){
        this.isOffline = true;
        // keep existing (mock) services; small random jitter can be added here if desired
      }
    },

    async fetchEvents(){
      try{
        let res = await fetch('/api/events?limit=10');
        if(res.status === 401){
          this.currentUser = null;
          this.isAdmin = false;
          return;
        }
        if(!res.ok) throw new Error('no');
        let data = await res.json();
        this.events = data.events;
        this.isOffline = false;
      }catch(e){
        this.isOffline = true;
      }
    },

    async fetchResources(){
      try{
        let res = await fetch('/api/services/resource-breakdown');
        if(!res.ok) throw new Error('no');
        let data = await res.json();
        this.resources = data;
        this.isOffline = false;
      }catch(e){
        this.isOffline = true;
        this.mockResourcesUpdate();
      }
    },

    mockStatsUpdate(){
      this.stats.cpu_percent = clamp(this.stats.cpu_percent + rand(-2,2), 0, 99);
      this.stats.ram_percent = clamp(this.stats.ram_percent + rand(-1,1), 0, 99);
      this.stats.disk_percent = clamp(this.stats.disk_percent + rand(-0.5,0.5), 0, 99);
      this.stats.updated_at = new Date().toISOString();
    },

    mockResourcesUpdate(){
      ['cpu_breakdown','ram_breakdown'].forEach(key=>{
        let arr = this.resources[key];
        let total = 0;
        arr.forEach(a=>{ a.percent = Math.max(0, Math.round((a.percent + rand(-1,1)) * 10)/10); total += a.percent; });
        if(total <= 0) return;
        arr.forEach(a=>{ a.percent = Math.round((a.percent/total*100) * 10)/10; });
      });
    },

    async startService(svc){
      if(svc.status !== 'stopped') return;
      svc._error = '';
      svc._starting = true;
      svc.status = 'starting';
      try{
        let res = await fetch('/api/services/' + encodeURIComponent(svc.name) + '/start', { method: 'POST' });
        if(!res.ok) throw new Error('server');
        let data = await res.json();
        if(data.status === 'starting'){
          // poll the services endpoint aggressively for this service until running
          let check = setInterval(async ()=>{
            try{
              let r = await fetch('/api/services');
              if(r.ok){ let p = await r.json(); let found = p.services.find(x=>x.name===svc.name); if(found && found.status === 'running'){ svc.status = 'running'; svc._starting = false; clearInterval(check); } }
            }catch(e){}
          }, 2000);
        }else{ svc.status = 'running'; svc._starting = false; }
      }catch(e){
        // fallback mock simulation
        setTimeout(()=>{
          svc.status = 'running';
          svc._starting = false;
          if(!svc.url) svc.url = '/' + svc.name + '/';
          this.events.unshift({ id: Date.now(), type: 'service_started', service: svc.name, message: svc.display_name + ' paleistas', timestamp: new Date().toISOString() });
        }, 3000);
      }
    },

    statusText(status){ switch(status){ case 'running': return 'Veikia'; case 'stopped': return 'Miega'; case 'starting': return 'Paleidžiama...'; case 'error': return 'Klaida'; default: return status; } },
    badgeClass(status){ return (status === 'running' ? 'text-emerald-400' : status === 'stopped' ? 'text-zinc-500' : status === 'starting' ? 'text-amber-400' : 'text-red-400'); },
    dotClass(status){ return (status === 'running' ? 'bg-emerald-400' : status === 'stopped' ? 'bg-zinc-500' : status === 'starting' ? 'bg-amber-400' : 'bg-red-400'); },

    formatTimestamp(ts){ if(!ts) return ''; let d = new Date(ts); return d.toLocaleString(); },
    relativeTime(ts){ if(!ts) return ''; let d = new Date(ts); let sec = Math.floor((Date.now() - d.getTime())/1000); if(sec < 60) return 'prieš ' + sec + ' s'; if(sec < 3600) return 'prieš ' + Math.floor(sec/60) + ' min'; return 'prieš ' + Math.floor(sec/3600) + ' h'; },
    formatBytes(bytes){ if(bytes >= 1024*1024*1024) return (bytes/(1024*1024*1024)).toFixed(1)+' GiB'; if(bytes >= 1024*1024) return (bytes/(1024*1024)).toFixed(1)+' MiB'; return bytes + ' B'; },

    totalPercent(arr){ let s = 0; arr.forEach(i=>s += i.percent); return Math.round(s*10)/10; },
    stackedOffset(arr, item){ let off = 0; for(let a of arr){ if(a === item) break; off += a.percent; } return off; },
    cardAccentClass(idx){ const accents = ['','']; return accents[idx % accents.length]; },

    // Lucide ikonų vardai įvykių tipams
    iconForEvent(ev){
      switch(ev.type){
        case 'service_started': return 'zap';
        case 'service_stopped': return 'square';
        case 'service_sleep':   return 'moon';
        case 'service_error':   return 'alert-circle';
        default:                return 'info';
      }
    },
    iconBgForEvent(ev){
      switch(ev.type){
        case 'service_started': return 'bg-emerald-500/10 border border-emerald-500/20';
        case 'service_stopped':
        case 'service_sleep':   return 'bg-zinc-700/30 border border-zinc-600/20';
        case 'service_error':   return 'bg-red-500/10 border border-red-500/20';
        default:                return 'bg-zinc-800';
      }
    },
    iconColorForEvent(ev){
      switch(ev.type){
        case 'service_started': return 'text-emerald-400';
        case 'service_stopped':
        case 'service_sleep':   return 'text-zinc-400';
        case 'service_error':   return 'text-red-400';
        default:                return 'text-zinc-300';
      }
    },

    // Lucide ikonų rendering - kviečiam po kiekvieno DOM atnaujinimo
    renderIcons(){
      if(window.lucide){
        try { lucide.createIcons(); } catch(e) {}
      }
    },
    uptimeString(){ let s = this.serverInfo.uptime_seconds || 0; let days = Math.floor(s/86400); let hours = Math.floor((s % 86400)/3600); return days + ' d, ' + hours + ' h'; },

    formatSleepCountdown(seconds){
      if(seconds == null) return '';
      if(seconds < 60) return seconds + 's';
      if(seconds < 3600) return Math.floor(seconds/60) + ' min';
      let h = Math.floor(seconds/3600);
      let m = Math.floor((seconds % 3600) / 60);
      return h + 'h ' + m + 'min';
    }
  };
}

window.dashboard = dashboard;
