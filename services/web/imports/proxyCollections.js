// TreeGent web — proxy-facing collections (shared DB, read-only via Meteor;
// writes go through the proxy admin API to keep a single writer).
import { Mongo } from 'meteor/mongo';

export const TaskClasses = new Mongo.Collection('task_classes');
export const Providers = new Mongo.Collection('providers');
export const ModelCatalog = new Mongo.Collection('model_catalog');
export const UsageEvents = new Mongo.Collection('usage_events');
