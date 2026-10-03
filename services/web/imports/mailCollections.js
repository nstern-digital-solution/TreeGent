// TreeGent web — mail/approvals collections (shared DB, reactive reads)
import { Mongo } from 'meteor/mongo';

export const Mailboxes = new Mongo.Collection('mailboxes');
export const MailMessages = new Mongo.Collection('mail_messages');
export const Approvals = new Mongo.Collection('approvals');
