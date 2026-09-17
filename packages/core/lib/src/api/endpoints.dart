/// Every path the apps use, in one place, so a rename is one edit rather than a search.
class Endpoints {
  static const appConfig = '/api/v1/app/config';
  static const appHome = '/api/v1/app/home';

  static const login = '/api/v1/auth/login';
  static const register = '/api/v1/auth/register';
  static const refresh = '/api/v1/auth/refresh';
  static const otpRequest = '/api/v1/auth/otp/request';
  static const otpVerify = '/api/v1/auth/otp/verify';

  static const cart = '/api/v1/cart';
  static const cartItems = '/api/v1/cart/items';
  static const quote = '/api/v1/cart/quote';
  static const place = '/api/v1/checkout/place';
  static const addresses = '/api/v1/me/addresses';
  static const districts = '/api/v1/geo/districts';

  static const orders = '/api/v1/me/orders';
  static const returns = '/api/v1/me/returns';
  static const wallet = '/api/v1/me/store-credit';
  static const notifications = '/api/v1/me/notifications';
  static const devices = '/api/v1/me/devices';
  static const tickets = '/api/v1/me/tickets';
  static const accountDelete = '/api/v1/app/account/delete';

  static const search = '/api/v1/catalog/search';
  static String product(String slug) => '/api/v1/catalog/products/$slug';
  static String order(String number) => '/api/v1/me/orders/$number';
  static String tracking(String number) => '/api/v1/me/orders/$number/tracking';
  static String payment(String id) => '/api/v1/payments/$id';
  static String startPayment() => '/api/v1/payments/start';
  static String confirmPayment(String id) => '/api/v1/payments/$id/confirm';
  static String reviews(String productId) => '/api/v1/products/$productId/reviews';
}
