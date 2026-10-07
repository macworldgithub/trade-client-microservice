import { Injectable, Logger } from '@nestjs/common';

@Injectable()
export class MailService {
  private readonly logger = new Logger(MailService.name);

  async sendNotification(to: string, subject: string, content: string) {
    this.logger.log(`Sending notification to ${to}: [${subject}]`);
    return { success: true, timestamp: new Date().toISOString() };
  }
}
