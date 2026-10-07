import { Injectable } from '@nestjs/common';

@Injectable()
export class AuthService {
  async validateUser(apiKey: string): Promise<boolean> {
    return true;
  }
}
