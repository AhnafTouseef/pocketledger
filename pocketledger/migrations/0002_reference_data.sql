-- Reference data and default settings (not sample data).

INSERT INTO currencies (code, name, symbol, minor_unit, grouping, is_enabled)
VALUES ('BDT', 'Bangladeshi Taka', '৳', 2, 'indian', 1);

INSERT INTO account_categories (name, kind, icon, sort_order) VALUES
    ('Physical cash', 'physical', 'banknote', 1),
    ('Bank account',  'bank',     'landmark', 2),
    ('Mobile wallet', 'wallet',   'smartphone', 3),
    ('Other',         'other',    'wallet', 4);

INSERT INTO transaction_categories (name, kind, sort_order) VALUES
    ('Salary',            'income',  1),
    ('Freelance',         'income',  2),
    ('Gift',              'income',  3),
    ('Refund',            'income',  4),
    ('Other income',      'income',  99),
    ('Food',              'expense', 1),
    ('Transport',         'expense', 2),
    ('Bills & utilities', 'expense', 3),
    ('Rent',              'expense', 4),
    ('Shopping',          'expense', 5),
    ('Health',            'expense', 6),
    ('Education',         'expense', 7),
    ('Entertainment',     'expense', 8),
    ('Mobile & internet', 'expense', 9),
    ('Other expense',     'expense', 99);

INSERT INTO application_settings (key, value) VALUES
    ('base_currency',        'BDT'),
    ('timezone',             'Asia/Dhaka'),
    ('week_start',           'sunday'),
    ('verify_reminder_days', '7'),
    ('due_soon_days',        '7'),
    ('is_demo',              '0');
