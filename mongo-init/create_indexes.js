// Runs once on a fresh MongoDB volume (docker-entrypoint-initdb.d behaviour).
// Creates indexes on all join-key fields used by $lookup aggregations.
// Without these indexes every cross-collection query does a nested-loop scan
// (O(n*m)) and will timeout on 100k-row collections.

db = db.getSiblingDB('ecommerce_db');

db.olist_order_items_dataset.createIndex({ order_id:   1 });
db.olist_order_items_dataset.createIndex({ product_id: 1 });
db.olist_order_items_dataset.createIndex({ seller_id:  1 });

db.olist_orders_dataset.createIndex({ order_id:   1 });
db.olist_orders_dataset.createIndex({ customer_id: 1 });

db.olist_sellers_dataset.createIndex({ seller_id: 1 });

db.olist_products_dataset.createIndex({ product_id:            1 });
db.olist_products_dataset.createIndex({ product_category_name: 1 });

db.olist_order_payments_dataset.createIndex({ order_id: 1 });

db.olist_order_reviews_dataset.createIndex({ order_id: 1 });

db.olist_customers_dataset.createIndex({ customer_id: 1 });

db.product_category_name_translation.createIndex({ product_category_name: 1 });

print('NL Insight: join-key indexes created on ecommerce_db');

// ─────────────────────────────────────────────────────────────────────────────
// retail_warehouse_db — "Retail Data Warehouse – 12 Table 1M+ Rows Dataset"
// (https://www.kaggle.com/datasets/datarspectrum/retail-data-warehouse-12-table-1m-rows-dataset)
//
// Confirmed against the actual CSV headers (2026-07-08):
//   categories(category_id, category_name)
//   customers(customer_id, city, signup_date)
//   employees(employee_id, store_id, salary)
//   order_items(order_item_id, order_id, product_id, qty, price)
//   orders(order_id, customer_id, store_id, order_date, promotion_id)
//   payments(payment_id, order_id, amount)
//   products(product_id, category_id, supplier_id, price)
//   promotions(promotion_id, discount)
//   returns(return_id, order_item_id, refund)
//   shipments(shipment_id, order_id, status)
//   stores(store_id, city)
//   suppliers(supplier_id, country)
// ─────────────────────────────────────────────────────────────────────────────

db = db.getSiblingDB('retail_warehouse_db');

db.customers.createIndex({ customer_id: 1 });

db.stores.createIndex({ store_id: 1 });

db.employees.createIndex({ employee_id: 1 });
db.employees.createIndex({ store_id: 1 });

db.categories.createIndex({ category_id: 1 });

db.suppliers.createIndex({ supplier_id: 1 });

db.products.createIndex({ product_id: 1 });
db.products.createIndex({ category_id: 1 });
db.products.createIndex({ supplier_id: 1 });

db.promotions.createIndex({ promotion_id: 1 });

db.orders.createIndex({ order_id: 1 });
db.orders.createIndex({ customer_id: 1 });
db.orders.createIndex({ store_id: 1 });
db.orders.createIndex({ promotion_id: 1 });

db.order_items.createIndex({ order_item_id: 1 });
db.order_items.createIndex({ order_id: 1 });
db.order_items.createIndex({ product_id: 1 });

db.payments.createIndex({ order_id: 1 });

db.shipments.createIndex({ order_id: 1 });

db.returns.createIndex({ order_item_id: 1 });

print('NL Insight: join-key indexes created on retail_warehouse_db');
