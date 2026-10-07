import { Injectable } from '@nestjs/common';

@Injectable()
export class UsersService {
  private users = [
    { id: '1', name: 'Admin', email: 'admin@tradie.com', role: 'admin' },
  ];

  async findAll() {
    return this.users;
  }

  async findById(id: string) {
    return this.users.find((u) => u.id === id) || null;
  }
}
