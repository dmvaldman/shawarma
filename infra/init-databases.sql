\getenv harness_password HARNESS_DB_PASSWORD
CREATE ROLE harness LOGIN PASSWORD :'harness_password';
CREATE DATABASE harness OWNER harness;
