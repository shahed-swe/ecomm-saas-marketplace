// Multi-tenant load profile.
//
// The point is not a big number: it is that one busy tenant must not slow another one down, and
// that the paths which take money keep their latency under load. So the script runs three tenants
// at once with different mixes and reports thresholds per tenant.
//
//   k6 run -e BASE=https://api.example.com -e HOSTS=a.example.com,b.example.com,c.example.com \
//          infra/load/k6-marketplace.js
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';

const BASE = __ENV.BASE || 'http://localhost:8000';
const HOSTS = (__ENV.HOSTS || 'shop-a.localhost,shop-b.localhost,shop-c.localhost').split(',');

const browse = new Trend('browse_ms', true);
const search = new Trend('search_ms', true);
const pdp = new Trend('pdp_ms', true);

export const options = {
  scenarios: {
    // A steady shopfront: mostly browsing, the traffic every tenant has all day.
    browsing: {
      executor: 'ramping-vus',
      startVUs: 5,
      stages: [
        { duration: '1m', target: 50 },
        { duration: '3m', target: 50 },
        { duration: '1m', target: 0 },
      ],
      exec: 'browsing',
    },
    // One tenant running a flash sale while the others carry on: the noisy-neighbour test.
    flash_sale: {
      executor: 'constant-arrival-rate',
      rate: 40,
      timeUnit: '1s',
      duration: '3m',
      preAllocatedVUs: 100,
      exec: 'flashSale',
      startTime: '1m',
    },
  },
  thresholds: {
    // Storefront reads stay fast even while a neighbour is being hammered.
    'browse_ms{tenant:0}': ['p(95)<400'],
    'browse_ms{tenant:1}': ['p(95)<400'],
    'search_ms': ['p(95)<500'],
    'pdp_ms': ['p(95)<400'],
    'http_req_failed': ['rate<0.01'],
  },
};

function headers(index) {
  return { headers: { Host: HOSTS[index % HOSTS.length] }, tags: { tenant: String(index % HOSTS.length) } };
}

export function browsing() {
  const index = __VU % HOSTS.length;
  const options_ = headers(index);
  const home = http.get(`${BASE}/api/v1/storefront/page?template=home`, options_);
  browse.add(home.timings.duration, { tenant: String(index) });
  check(home, { 'home ok': (r) => r.status === 200 });

  const results = http.get(`${BASE}/api/v1/catalog/search?q=kurti`, options_);
  search.add(results.timings.duration, { tenant: String(index) });
  check(results, { 'search ok': (r) => r.status === 200 });

  const body = results.json();
  const items = (body && (body.items || body.results)) || [];
  if (items.length > 0) {
    const detail = http.get(`${BASE}/api/v1/catalog/products/${items[0].slug}`, options_);
    pdp.add(detail.timings.duration, { tenant: String(index) });
    check(detail, { 'pdp ok': (r) => r.status === 200 });
  }
  sleep(Math.random() * 2);
}

// Tenant 0 only: everyone piling onto one campaign page and one product.
export function flashSale() {
  const options_ = headers(0);
  const page = http.get(`${BASE}/api/v1/storefront/page?template=home`, options_);
  browse.add(page.timings.duration, { tenant: '0' });
  http.get(`${BASE}/api/v1/catalog/search?q=sale&sort=newest`, options_);
  sleep(0.2);
}
