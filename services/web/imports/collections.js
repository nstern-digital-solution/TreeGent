// Shared collection definitions — both client and server import these.
// The collections live in the SAME MongoDB database the chat service writes
// to (MONGO_URL points there). Web reads reactively via Meteor; ALL writes
// go through the chat service HTTP API (single source of truth).
import { Mongo } from 'meteor/mongo';

export const Actors = new Mongo.Collection('actors');
export const Conversations = new Mongo.Collection('conversations');
export const Messages = new Mongo.Collection('messages');
export const Inbox = new Mongo.Collection('inbox');
export const AgentTranscripts = new Mongo.Collection('agent_transcripts');
export const Jobs = new Mongo.Collection('jobs');
