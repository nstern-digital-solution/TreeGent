import { Mongo } from 'meteor/mongo';

export const Secrets = new Mongo.Collection('secrets');
export const Files = new Mongo.Collection('files');
export const RuntimeTurns = new Mongo.Collection('runtime_turns');
